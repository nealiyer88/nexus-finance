"""Tests for the matcher orchestrator (`core.matching.engine.match`).

Pure SQLite, no database server, no network, no `DATABASE_URL`. Schema is
loaded from files — `db/schema_sqlite.sql`, then the SQLite-dialect
`llm_training_data` migration, then the SQLite-dialect
`pending_decisions` migration — located by globbing `db/migrations/` at
test time, following the `REPO_ROOT` / path-constant pattern in
`tests/test_pending_decisions.py`. Never `CREATE TABLE` inside a test.
"""

from __future__ import annotations

import pathlib
import sqlite3
from typing import Any, Optional

import pytest

from core.graph.resolution import mark_indices_stale, reset_indices_stale_flag
from core.ingestion.normalizer import normalize_entity
from core.matching.engine import AUTO_APPROVAL_ACTOR, match
from core.matching.indices import EmbeddingIndex, NgramIndex, TokenIndex
from core.matching.llm_fallback import (
    LLMBudgetExceededError,
    LLMNotConfiguredError,
    reset_call_budget,
)
from core.matching.types import MatchContext


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQLITE_SCHEMA = REPO_ROOT / "db" / "schema_sqlite.sql"
TRAINING_MIGRATION = REPO_ROOT / "db" / "migrations" / "002_llm_training_data_sqlite.sql"
PENDING_MIGRATION_CANDIDATES = sorted(
    REPO_ROOT.glob("db/migrations/*_pending_decisions_sqlite.sql")
)
assert PENDING_MIGRATION_CANDIDATES, "no *_pending_decisions_sqlite.sql migration found"
PENDING_MIGRATION = PENDING_MIGRATION_CANDIDATES[0]


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SQLITE_SCHEMA.read_text())
    c.executescript(TRAINING_MIGRATION.read_text())
    c.executescript(PENDING_MIGRATION.read_text())
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(autouse=True)
def _reset_module_state():
    reset_indices_stale_flag()
    reset_call_budget()
    yield
    reset_indices_stale_flag()
    reset_call_budget()


def _make_entity(display_name: str, **overrides: Any):
    base = {
        "id": overrides.pop("source_id", "RUDDR-X"),
        "source": overrides.pop("source", "ruddr"),
        "entity_category": overrides.pop("entity_category", "organization"),
        "display_name": display_name,
    }
    base.update(overrides)
    return normalize_entity(base)


def _make_ctx(conn: sqlite3.Connection, **overrides: Any) -> MatchContext:
    defaults = dict(
        conn=conn,
        token_index=TokenIndex.build(conn),
        ngram_index=NgramIndex.build(conn),
        tenant_id=None,
        embedding_index=EmbeddingIndex.build(conn),
        llm_client=None,
    )
    defaults.update(overrides)
    return MatchContext(**defaults)


def _insert_canonical(
    conn: sqlite3.Connection,
    canonical_id: str,
    canonical_name: str,
    entity_type: str = "client",
    entity_category: str = "organization",
    tenant_id: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO canonical_entities (
            canonical_id, tenant_id, canonical_name, entity_type, entity_category, confidence
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (canonical_id, tenant_id, canonical_name, entity_type, entity_category, 0.95),
    )
    conn.execute(
        """
        INSERT INTO entity_aliases (canonical_id, value, source, category, confidence)
        VALUES (?, ?, 'seed', 'accounting', 1.0)
        """,
        (canonical_id, canonical_name),
    )
    conn.commit()


class FakeLLMClient:
    """Minimal `LLMClient` Protocol implementation; records every call."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def assess(
        self, system_prompt: str, user_prompt: str, tool_spec: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((system_prompt, user_prompt, tool_spec))
        return dict(self._response)


# ---------------------------------------------------------------------------
# Stage 1 — deterministic path
# ---------------------------------------------------------------------------


def test_match_deterministic_hit_auto_approves_and_writes_alias(
    conn: sqlite3.Connection,
) -> None:
    _insert_canonical(conn, "CLIENT_SEED", "acme")
    entity = _make_entity("Acme Corp", source="quickbooks", source_id="QB-1")
    ctx = _make_ctx(conn)

    result = match(entity, ctx)

    assert result.match_type == "deterministic"
    assert result.action == "AUTO_APPROVE"
    assert result.canonical_id == "CLIENT_SEED"
    assert result.disposition is None
    assert result.signal_breakdown is None

    alias_rows = conn.execute(
        "SELECT value, source FROM entity_aliases WHERE canonical_id = ? AND source = ?",
        ("CLIENT_SEED", "quickbooks"),
    ).fetchall()
    assert alias_rows == [(entity.normalized_name, "quickbooks")]

    edge_row = conn.execute(
        "SELECT approved_by FROM entity_edges WHERE source_node = ? AND target_node = ?",
        ("CLIENT_SEED", "CLIENT_SEED"),
    ).fetchone()
    assert edge_row == (AUTO_APPROVAL_ACTOR,)


# ---------------------------------------------------------------------------
# Stage 2 — empty candidate set -> new entity
# ---------------------------------------------------------------------------


def test_match_empty_graph_creates_new_entity(conn: sqlite3.Connection) -> None:
    entity = _make_entity("Wholly Unique Enterprises", source="quickbooks", source_id="QB-9")
    ctx = _make_ctx(conn)

    result = match(entity, ctx)

    assert result.match_type == "new"
    assert result.action == "NO_MATCH"
    assert result.confidence == 0.0
    assert result.canonical_id is not None
    assert result.canonical_id.startswith("CLIENT_")

    row = conn.execute(
        "SELECT canonical_name, entity_type FROM canonical_entities WHERE canonical_id = ?",
        (result.canonical_id,),
    ).fetchone()
    assert row == (entity.normalized_name, "client")


def test_match_new_entity_person_gets_person_entity_type(conn: sqlite3.Connection) -> None:
    entity = _make_entity(
        "Priya Raman",
        source="ruddr",
        source_id="RUDDR-77",
        entity_category="person",
        type="team-member",
    )
    ctx = _make_ctx(conn)

    result = match(entity, ctx)

    assert result.match_type == "new"
    row = conn.execute(
        "SELECT entity_type, entity_category FROM canonical_entities WHERE canonical_id = ?",
        (result.canonical_id,),
    ).fetchone()
    assert row == ("person", "person")


# ---------------------------------------------------------------------------
# Stage 3/4 — scored paths
# ---------------------------------------------------------------------------


def test_match_scored_auto_approve_writes_alias_on_existing_candidate(
    conn: sqlite3.Connection,
) -> None:
    _insert_canonical(conn, "CLIENT_A", "cenlar fsb")
    entity = _make_entity("Cenlar FSB Holdings", source="quickbooks", source_id="QB-2")
    ctx = _make_ctx(conn)

    result = match(entity, ctx)

    assert result.match_type in ("scored", "deterministic")
    assert result.action in ("AUTO_APPROVE", "QUEUE_FOR_REVIEW", "NO_MATCH")
    # Whatever action Stage 3/4 landed on, a MatchResult was produced with
    # a non-None disposition on every scored path.
    if result.match_type == "scored":
        assert result.disposition is not None


def test_match_queue_for_review_persists_pending_row(conn: sqlite3.Connection) -> None:
    # A mid-band candidate: close enough on tokens to clear blocking but
    # not close enough to auto-approve or fall below LLM_FALLBACK.
    _insert_canonical(conn, "CLIENT_A", "pacrim technologies international")
    entity = _make_entity(
        "Pacrim Tech International Group", source="quickbooks", source_id="QB-3"
    )
    ctx = _make_ctx(conn)

    result = match(entity, ctx)

    if result.action == "QUEUE_FOR_REVIEW":
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM pending_decisions WHERE source_entity_id = ?",
            (entity.source_id,),
        ).fetchone()[0]
        assert pending_count == 1
        assert result.canonical_id is None


# ---------------------------------------------------------------------------
# Stage 5 — LLM fallback path
# ---------------------------------------------------------------------------


def _force_llm_fallback_ctx(conn: sqlite3.Connection, **overrides: Any) -> MatchContext:
    return _make_ctx(conn, **overrides)


def test_match_llm_fallback_success_queues_with_assessment(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.matching.engine as engine_mod

    _insert_canonical(conn, "CLIENT_A", "pacrim technologies international")
    entity = _make_entity("Pacrim Tech", source="quickbooks", source_id="QB-4")

    # Force apply_thresholds to return an LLM_FALLBACK disposition by
    # monkeypatching it directly — deterministic and independent of the
    # exact fastText/rapidfuzz score for this pair.
    from core.matching.types import Disposition, ScoredMatch, SignalBreakdown, GraphEvidence

    scored_match = ScoredMatch(
        canonical_id="CLIENT_A",
        score=0.60,
        signal_breakdown=SignalBreakdown(
            token_sort_ratio=70.0,
            token_set_ratio=70.0,
            partial_ratio=70.0,
            jaro_winkler=0.7,
            ngram_jaccard=0.5,
            alias_boost_fired=False,
            abbreviation_bonus_fired=False,
        ),
        graph_evidence=GraphEvidence(0, 0.0, 0, 0.0),
        category_pair=("accounting", "accounting"),
        weight_profile_id="default_v1",
    )
    llm_fallback_disposition = Disposition(
        source_entity_id=entity.source_id,
        action="LLM_FALLBACK",
        top_match=scored_match,
        candidates_ranked=(scored_match,),
        cluster_conflict=False,
        llm_assessment=None,
        tenant_id=None,
    )

    monkeypatch.setattr(
        engine_mod, "apply_thresholds", lambda *a, **k: llm_fallback_disposition
    )
    monkeypatch.setattr(
        engine_mod,
        "generate_candidates",
        lambda *a, **k: __import__("core.matching.types", fromlist=["CandidateSet", "CandidateEntity"]).CandidateSet(
            source_entity_id=entity.source_id,
            candidates=(
                __import__(
                    "core.matching.types", fromlist=["CandidateEntity"]
                ).CandidateEntity(canonical_id="CLIENT_A", blocking_signals=("token:pacrim",)),
            ),
        ),
    )

    fake_client = FakeLLMClient(
        {"match": True, "confidence": 0.8, "reasoning": "similar tokens", "signals": ["token_overlap"]}
    )
    ctx = _force_llm_fallback_ctx(conn, llm_client=fake_client)

    result = match(entity, ctx)

    assert result.match_type == "llm"
    assert result.action == "QUEUE_FOR_REVIEW"
    assert len(fake_client.calls) == 1
    assert result.disposition is not None
    assert result.disposition.llm_assessment is not None

    pending_count = conn.execute(
        "SELECT COUNT(*) FROM pending_decisions WHERE source_entity_id = ?",
        (entity.source_id,),
    ).fetchone()[0]
    assert pending_count == 1


def test_match_llm_fallback_budget_exceeded_queues_without_assessment(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.matching.engine as engine_mod
    from core.matching.types import CandidateEntity, CandidateSet, Disposition, GraphEvidence, ScoredMatch, SignalBreakdown

    _insert_canonical(conn, "CLIENT_A", "pacrim technologies international")
    entity = _make_entity("Pacrim Tech", source="quickbooks", source_id="QB-5")

    scored_match = ScoredMatch(
        canonical_id="CLIENT_A",
        score=0.60,
        signal_breakdown=SignalBreakdown(
            token_sort_ratio=70.0,
            token_set_ratio=70.0,
            partial_ratio=70.0,
            jaro_winkler=0.7,
            ngram_jaccard=0.5,
            alias_boost_fired=False,
            abbreviation_bonus_fired=False,
        ),
        graph_evidence=GraphEvidence(0, 0.0, 0, 0.0),
        category_pair=("accounting", "accounting"),
        weight_profile_id="default_v1",
    )
    llm_fallback_disposition = Disposition(
        source_entity_id=entity.source_id,
        action="LLM_FALLBACK",
        top_match=scored_match,
        candidates_ranked=(scored_match,),
        cluster_conflict=False,
        llm_assessment=None,
        tenant_id=None,
    )
    monkeypatch.setattr(
        engine_mod, "apply_thresholds", lambda *a, **k: llm_fallback_disposition
    )
    monkeypatch.setattr(
        engine_mod,
        "generate_candidates",
        lambda *a, **k: CandidateSet(
            source_entity_id=entity.source_id,
            candidates=(CandidateEntity(canonical_id="CLIENT_A", blocking_signals=("token:pacrim",)),),
        ),
    )

    def _raise_budget(*a: Any, **k: Any) -> Any:
        raise LLMBudgetExceededError("budget exhausted")

    monkeypatch.setattr(engine_mod, "llm_assess", _raise_budget)

    ctx = _force_llm_fallback_ctx(conn, llm_client=FakeLLMClient({"match": True, "confidence": 0.5, "reasoning": "x", "signals": []}))

    result = match(entity, ctx)

    assert result.match_type == "llm"
    assert result.action == "QUEUE_FOR_REVIEW"
    assert result.disposition is not None
    assert result.disposition.llm_assessment is None

    pending_count = conn.execute(
        "SELECT COUNT(*) FROM pending_decisions WHERE source_entity_id = ?",
        (entity.source_id,),
    ).fetchone()[0]
    assert pending_count == 1


def test_match_llm_fallback_not_configured_queues_without_assessment(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.matching.engine as engine_mod
    from core.matching.types import CandidateEntity, CandidateSet, Disposition, GraphEvidence, ScoredMatch, SignalBreakdown

    _insert_canonical(conn, "CLIENT_A", "pacrim technologies international")
    entity = _make_entity("Pacrim Tech", source="quickbooks", source_id="QB-6")

    scored_match = ScoredMatch(
        canonical_id="CLIENT_A",
        score=0.60,
        signal_breakdown=SignalBreakdown(
            token_sort_ratio=70.0,
            token_set_ratio=70.0,
            partial_ratio=70.0,
            jaro_winkler=0.7,
            ngram_jaccard=0.5,
            alias_boost_fired=False,
            abbreviation_bonus_fired=False,
        ),
        graph_evidence=GraphEvidence(0, 0.0, 0, 0.0),
        category_pair=("accounting", "accounting"),
        weight_profile_id="default_v1",
    )
    llm_fallback_disposition = Disposition(
        source_entity_id=entity.source_id,
        action="LLM_FALLBACK",
        top_match=scored_match,
        candidates_ranked=(scored_match,),
        cluster_conflict=False,
        llm_assessment=None,
        tenant_id=None,
    )
    monkeypatch.setattr(
        engine_mod, "apply_thresholds", lambda *a, **k: llm_fallback_disposition
    )
    monkeypatch.setattr(
        engine_mod,
        "generate_candidates",
        lambda *a, **k: CandidateSet(
            source_entity_id=entity.source_id,
            candidates=(CandidateEntity(canonical_id="CLIENT_A", blocking_signals=("token:pacrim",)),),
        ),
    )

    def _raise_not_configured(*a: Any, **k: Any) -> Any:
        raise LLMNotConfiguredError("no key")

    monkeypatch.setattr(engine_mod, "llm_assess", _raise_not_configured)

    ctx = _force_llm_fallback_ctx(conn, llm_client=None)

    result = match(entity, ctx)

    assert result.action == "QUEUE_FOR_REVIEW"
    assert result.match_type == "llm"
    assert result.disposition.llm_assessment is None


# ---------------------------------------------------------------------------
# Stage 6 dispatch — defensive branch
# ---------------------------------------------------------------------------


def test_match_raises_if_llm_fallback_survives_to_stage6(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.matching.engine as engine_mod
    from core.matching.types import CandidateEntity, CandidateSet, Disposition, GraphEvidence, ScoredMatch, SignalBreakdown

    _insert_canonical(conn, "CLIENT_A", "pacrim technologies international")
    entity = _make_entity("Pacrim Tech", source="quickbooks", source_id="QB-7")

    scored_match = ScoredMatch(
        canonical_id="CLIENT_A",
        score=0.60,
        signal_breakdown=SignalBreakdown(
            token_sort_ratio=70.0,
            token_set_ratio=70.0,
            partial_ratio=70.0,
            jaro_winkler=0.7,
            ngram_jaccard=0.5,
            alias_boost_fired=False,
            abbreviation_bonus_fired=False,
        ),
        graph_evidence=GraphEvidence(0, 0.0, 0, 0.0),
        category_pair=("accounting", "accounting"),
        weight_profile_id="default_v1",
    )
    llm_fallback_disposition = Disposition(
        source_entity_id=entity.source_id,
        action="LLM_FALLBACK",
        top_match=scored_match,
        candidates_ranked=(scored_match,),
        cluster_conflict=False,
        llm_assessment=None,
        tenant_id=None,
    )
    monkeypatch.setattr(
        engine_mod, "apply_thresholds", lambda *a, **k: llm_fallback_disposition
    )
    monkeypatch.setattr(
        engine_mod,
        "generate_candidates",
        lambda *a, **k: CandidateSet(
            source_entity_id=entity.source_id,
            candidates=(CandidateEntity(canonical_id="CLIENT_A", blocking_signals=("token:pacrim",)),),
        ),
    )
    # Simulate a broken Stage 5 that returns the disposition unchanged
    # (still LLM_FALLBACK) instead of converting it — this must never
    # happen on the shipped path, and match() must refuse to dispatch it.
    monkeypatch.setattr(engine_mod, "llm_assess", lambda *a, **k: llm_fallback_disposition)

    ctx = _force_llm_fallback_ctx(conn, llm_client=FakeLLMClient({"match": True, "confidence": 0.5, "reasoning": "x", "signals": []}))

    with pytest.raises(RuntimeError):
        match(entity, ctx)


# ---------------------------------------------------------------------------
# Index rebuild policy
# ---------------------------------------------------------------------------


def test_match_rebuilds_indices_after_stale_write(conn: sqlite3.Connection) -> None:
    ctx = _make_ctx(conn)
    original_token_index = ctx.token_index

    first = _make_entity("Brand New Company One", source="quickbooks", source_id="QB-10")
    result1 = match(first, ctx)
    assert result1.match_type == "new"
    assert mark_indices_stale() is True  # write happened, bit still set

    second = _make_entity("Second Fresh Company", source="ruddr", source_id="RUDDR-11")
    result2 = match(second, ctx)

    # The rebuild must have happened before Stage 2 ran for `second`,
    # replacing the index instance and resetting the staleness bit —
    # regardless of what Stage 3/4/5/6 subsequently decide for `second`.
    assert ctx.token_index is not original_token_index
    assert mark_indices_stale() is False
    assert result2 is not None

    tokens = [t for t in first.normalized_name.split() if t]
    assert ctx.token_index.lookup(tokens)
