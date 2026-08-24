"""Tests for the Stage 4 -> approval-queue handoff (`core.matching.pending_store`).

Pure SQLite, no database server, no network, no `DATABASE_URL`. Schema is
loaded from files — `db/schema_sqlite.sql`, then
`db/migrations/002_llm_training_data_sqlite.sql`, then this feature's
`db/migrations/004_pending_decisions_sqlite.sql` — following the
`SQLITE_SCHEMA` / `TRAINING_MIGRATION` path-constant pattern in
`tests/test_resolution.py` / `tests/test_llm_fallback.py`. Never
`CREATE TABLE` inside a test.
"""

from __future__ import annotations

import inspect
import json
import pathlib
import re
import sqlite3
import subprocess
import typing
from typing import Any, Optional

import pytest

from core.graph.resolution import create_new_entity, reject_match, resolve_match
from core.ingestion.normalizer import normalize_entity
from core.matching.pending_store import (
    PendingDecision,
    enqueue_pending,
    get_pending,
    list_pending,
    mark_decided,
    rehydrate,
)
from core.matching.scoring import BoostEntry
from core.matching.types import (
    Action,
    Disposition,
    GraphEvidence,
    LLMAssessment,
    ScoredMatch,
    SignalBreakdown,
)


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQLITE_SCHEMA = REPO_ROOT / "db" / "schema_sqlite.sql"
TRAINING_MIGRATION = REPO_ROOT / "db" / "migrations" / "002_llm_training_data_sqlite.sql"
PENDING_MIGRATION_CANDIDATES = sorted(
    REPO_ROOT.glob("db/migrations/*_pending_decisions_sqlite.sql")
)
assert PENDING_MIGRATION_CANDIDATES, "no *_pending_decisions_sqlite.sql migration found"
PENDING_MIGRATION = PENDING_MIGRATION_CANDIDATES[0]
PENDING_STORE_PATH = REPO_ROOT / "core" / "matching" / "pending_store.py"
TRAINING_DATA_PATH = REPO_ROOT / "core" / "matching" / "training_data.py"


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


def _insert_canonical(
    conn: sqlite3.Connection,
    canonical_id: str,
    canonical_name: str = "acme",
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
    conn.commit()


def _make_entity(display_name: str, **overrides: Any):
    base = {
        "id": overrides.pop("source_id", "RUDDR-X"),
        "source": overrides.pop("source", "ruddr"),
        "entity_category": overrides.pop("entity_category", "organization"),
        "display_name": display_name,
    }
    base.update(overrides)
    return normalize_entity(base)


def _make_scored_match(
    canonical_id: str,
    score: float,
    category_pair: tuple[str, str] = ("psa", "accounting"),
    with_boosts: bool = False,
) -> ScoredMatch:
    return ScoredMatch(
        canonical_id=canonical_id,
        score=score,
        signal_breakdown=SignalBreakdown(
            token_sort_ratio=90.0,
            token_set_ratio=90.0,
            partial_ratio=85.0,
            jaro_winkler=0.9,
            ngram_jaccard=0.8,
            alias_boost_fired=True,
            abbreviation_bonus_fired=False,
            fasttext_cosine=0.42,
            fasttext_available=True,
            b_boosts=(
                (BoostEntry(signal_id="B1", raw=0.05, applied=0.05),)
                if with_boosts
                else ()
            ),
        ),
        graph_evidence=GraphEvidence(
            shared_person_count=1 if with_boosts else 0,
            shared_person_bonus=0.05 if with_boosts else 0.0,
            neighborhood_overlap_count=0,
            neighborhood_overlap_bonus=0.0,
        ),
        category_pair=category_pair,
        weight_profile_id="default_v1",
    )


def _disposition(
    canonical_id: str = "CLIENT_0001",
    score: float = 0.80,
    source_entity_id: str = "src-1",
    action: str = "QUEUE_FOR_REVIEW",
    llm_assessment: Optional[LLMAssessment] = None,
    tenant_id: Optional[str] = None,
    with_boosts: bool = False,
    top_match: Optional[ScoredMatch] = "SENTINEL",  # type: ignore[assignment]
) -> Disposition:
    top = _make_scored_match(canonical_id, score, with_boosts=with_boosts) if top_match == "SENTINEL" else top_match
    return Disposition(
        source_entity_id=source_entity_id,
        action=action,
        top_match=top,
        candidates_ranked=(top,) if top is not None else (),
        cluster_conflict=False,
        llm_assessment=llm_assessment,
        tenant_id=tenant_id,
    )


def _llm_assessment(call_id: str = "call:abc123") -> LLMAssessment:
    return LLMAssessment(
        call_id=call_id,
        match=True,
        llm_confidence=0.65,
        reasoning="similar tokens",
        signals_examined=("category", "token_set_ratio"),
        prompt_sha256="deadbeef",
    )


def _default_proposal() -> dict[str, Any]:
    """A proposal dict carrying the union of required (non-defaulted,
    non-caller-supplied) keys across resolve_match / create_new_entity /
    reject_match, so the same fixture can drive any of the three."""
    return {
        "canonical_id": "CLIENT_0001",
        "alias_confidence": 0.9,
        "source_node": "CLIENT_0001",
        "target_node": "PROJECT_0001",
        "relationship": "HAS_PROJECT",
        "source_category": "psa",
        "target_category": "accounting",
        "weight": 0.9,
        "canonical_name": "acme",
        "entity_type": "client",
        "entity_category": "organization",
        "confidence": 0.9,
        "system_refs": ({"source": "ruddr", "category": "psa", "external_id": "RUDDR-X"},),
        "rejected_canonical_id": "CLIENT_0001",
    }


# ---------------------------------------------------------------------------
# 1. Import surface / migration DDL
# ---------------------------------------------------------------------------


def test_public_symbols_import() -> None:
    from core.matching.pending_store import (  # noqa: F401
        PendingDecision,
        enqueue_pending,
        get_pending,
        list_pending,
        mark_decided,
        rehydrate,
    )


def test_migration_creates_table_with_tenant_column_and_unique_decision_key(
    conn: sqlite3.Connection,
) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(pending_decisions)").fetchall()}
    assert "tenant_id" in cols

    indices = conn.execute("PRAGMA index_list(pending_decisions)").fetchall()
    unique_indices = [idx for idx in indices if idx[2] == 1]  # idx[2] == 'unique' flag
    covers_decision_key = False
    for idx in unique_indices:
        idx_name = idx[1]
        idx_cols = {r[2] for r in conn.execute(f"PRAGMA index_info({idx_name!r})").fetchall()}
        if "decision_key" in idx_cols:
            covers_decision_key = True
    assert covers_decision_key


# ---------------------------------------------------------------------------
# 2. Enqueue gate
# ---------------------------------------------------------------------------


def test_enqueue_gate_only_queue_for_review_writes(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_0001")
    entity = _make_entity("Meridian Consulting Group")

    all_actions = typing.get_args(Action)
    assert "QUEUE_FOR_REVIEW" in all_actions

    for action in all_actions:
        before = conn.execute("SELECT COUNT(*) FROM pending_decisions").fetchone()[0]
        disp = _disposition(action=action, top_match=None if action == "NO_MATCH" else "SENTINEL")
        pending_id = enqueue_pending(conn, disp, entity, _default_proposal())
        after = conn.execute("SELECT COUNT(*) FROM pending_decisions").fetchone()[0]
        if action == "QUEUE_FOR_REVIEW":
            assert pending_id is not None
            assert after == before + 1
        else:
            assert pending_id is None
            assert after == before


# ---------------------------------------------------------------------------
# 3. Round-trip equality
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_llm", [False, True])
def test_round_trip_equality(conn: sqlite3.Connection, with_llm: bool) -> None:
    _insert_canonical(conn, "CLIENT_0001")
    entity = _make_entity("Meridian Consulting Group")
    llm_assessment = _llm_assessment() if with_llm else None
    disp = _disposition(with_boosts=True, llm_assessment=llm_assessment)

    pid = enqueue_pending(conn, disp, entity, _default_proposal())
    assert pid is not None

    pending = get_pending(conn, pid)
    assert pending is not None

    rehydrated_disp, rehydrated_entity, _proposal = rehydrate(pending)

    assert rehydrated_disp == disp
    assert rehydrated_entity == entity
    assert isinstance(rehydrated_disp.candidates_ranked, tuple)
    assert isinstance(rehydrated_disp.candidates_ranked[0], ScoredMatch)
    assert isinstance(rehydrated_disp.candidates_ranked[0].signal_breakdown, SignalBreakdown)
    assert isinstance(rehydrated_disp.candidates_ranked[0].graph_evidence, GraphEvidence)
    for boost in rehydrated_disp.candidates_ranked[0].signal_breakdown.b_boosts:
        assert isinstance(boost, BoostEntry)
    if with_llm:
        assert isinstance(rehydrated_disp.llm_assessment, LLMAssessment)
    else:
        assert rehydrated_disp.llm_assessment is None


# ---------------------------------------------------------------------------
# 4. Signature coverage (derived, not enumerated)
# ---------------------------------------------------------------------------

_CALLER_SUPPLIED = {"conn", "disposition", "entity", "approved_by", "reasoning_trace", "tenant_id"}


@pytest.mark.parametrize("fn", [resolve_match, create_new_entity, reject_match])
def test_proposal_covers_required_stage6_arguments(conn: sqlite3.Connection, fn: Any) -> None:
    _insert_canonical(conn, "CLIENT_0001")
    entity = _make_entity("Meridian Consulting Group")
    disp = _disposition()
    pid = enqueue_pending(conn, disp, entity, _default_proposal())
    pending = get_pending(conn, pid)
    _rehydrated_disp, _rehydrated_entity, proposal = rehydrate(pending)

    params = inspect.signature(fn).parameters
    required = {
        name
        for name, p in params.items()
        if name not in _CALLER_SUPPLIED and p.default is inspect.Parameter.empty
    }
    assert required <= set(proposal.keys())


# ---------------------------------------------------------------------------
# 5. End-to-end handoff
# ---------------------------------------------------------------------------


def test_end_to_end_handoff_drives_resolve_match(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_0001", canonical_name="meridian consulting group")
    entity = _make_entity("Meridian Consulting Group", source_id="RUDDR-77")
    top = _make_scored_match("CLIENT_0001", 0.80)
    disp = Disposition(
        source_entity_id="src-77",
        action="QUEUE_FOR_REVIEW",
        top_match=top,
        candidates_ranked=(top,),
        cluster_conflict=False,
        llm_assessment=None,
        tenant_id=None,
    )
    proposal = {
        "canonical_id": "CLIENT_0001",
        "alias_confidence": 0.9,
        "source_node": "CLIENT_0001",
        "target_node": "CLIENT_0001",
        "relationship": "SAME_AS",
        "source_category": "psa",
        "target_category": "accounting",
        "weight": 0.9,
    }

    pid = enqueue_pending(conn, disp, entity, proposal)
    pending = get_pending(conn, pid)
    rehydrated_disp, rehydrated_entity, rehydrated_proposal = rehydrate(pending)

    resolve_match(
        conn,
        rehydrated_disp,
        rehydrated_entity,
        canonical_id=rehydrated_proposal["canonical_id"],
        alias_confidence=rehydrated_proposal["alias_confidence"],
        source_node=rehydrated_proposal["source_node"],
        target_node=rehydrated_proposal["target_node"],
        relationship=rehydrated_proposal["relationship"],
        source_category=rehydrated_proposal["source_category"],
        target_category=rehydrated_proposal["target_category"],
        weight=rehydrated_proposal["weight"],
        approved_by="human_reviewer",
    )

    alias_row = conn.execute(
        "SELECT value FROM entity_aliases WHERE canonical_id = ? AND value = ?",
        ("CLIENT_0001", entity.normalized_name),
    ).fetchone()
    assert alias_row is not None

    edge_row = conn.execute(
        """
        SELECT approval_count FROM entity_edges
         WHERE source_node = ? AND target_node = ? AND relationship = 'SAME_AS'
        """,
        ("CLIENT_0001", "CLIENT_0001"),
    ).fetchone()
    assert edge_row is not None
    assert edge_row[0] == 1


# ---------------------------------------------------------------------------
# 6. Idempotency
# ---------------------------------------------------------------------------


def test_idempotency_live_row_refreshes_in_place(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_0001")
    entity = _make_entity("Meridian Consulting Group")

    disp1 = _disposition(score=0.75)
    pid1 = enqueue_pending(conn, disp1, entity, _default_proposal())

    count_after_first = conn.execute("SELECT COUNT(*) FROM pending_decisions").fetchone()[0]

    disp2 = _disposition(score=0.82)
    pid2 = enqueue_pending(conn, disp2, entity, _default_proposal())

    count_after_second = conn.execute("SELECT COUNT(*) FROM pending_decisions").fetchone()[0]

    assert pid1 == pid2
    assert count_after_second == count_after_first

    row = conn.execute(
        "SELECT top_score FROM pending_decisions WHERE pending_id = ?", (pid1,)
    ).fetchone()
    assert row[0] == pytest.approx(0.82)


def test_idempotency_terminal_row_rejects_reenqueue(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_0001")
    entity = _make_entity("Meridian Consulting Group")

    disp = _disposition()
    pid = enqueue_pending(conn, disp, entity, _default_proposal())

    mark_decided(conn, pid, status="approved", resolved_by="human_reviewer", outcome_canonical_id="CLIENT_0001")

    before_row = conn.execute(
        "SELECT status, resolved_by, resolved_at FROM pending_decisions WHERE pending_id = ?",
        (pid,),
    ).fetchone()
    count_before = conn.execute("SELECT COUNT(*) FROM pending_decisions").fetchone()[0]

    disp2 = _disposition(score=0.95)
    result = enqueue_pending(conn, disp2, entity, _default_proposal())

    after_row = conn.execute(
        "SELECT status, resolved_by, resolved_at FROM pending_decisions WHERE pending_id = ?",
        (pid,),
    ).fetchone()
    count_after = conn.execute("SELECT COUNT(*) FROM pending_decisions").fetchone()[0]

    assert result is None
    assert count_after == count_before
    assert after_row == before_row


# ---------------------------------------------------------------------------
# 7. Tenant scoping
# ---------------------------------------------------------------------------


def test_tenant_scoping(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_A", tenant_id="tenant-a")
    _insert_canonical(conn, "CLIENT_B", tenant_id="tenant-b")

    entity_a = _make_entity("Alpha Corp", source_id="A-1")
    entity_b = _make_entity("Beta Corp", source_id="B-1")

    disp_a = _disposition(canonical_id="CLIENT_A", source_entity_id="src-a", tenant_id="tenant-a")
    disp_b = _disposition(canonical_id="CLIENT_B", source_entity_id="src-b", tenant_id="tenant-b")

    pid_a = enqueue_pending(conn, disp_a, entity_a, _default_proposal(), tenant_id="tenant-a")
    pid_b = enqueue_pending(conn, disp_b, entity_b, _default_proposal(), tenant_id="tenant-b")

    list_a = list_pending(conn, tenant_id="tenant-a")
    list_b = list_pending(conn, tenant_id="tenant-b")
    assert {p.pending_id for p in list_a} == {pid_a}
    assert {p.pending_id for p in list_b} == {pid_b}

    assert get_pending(conn, pid_a, tenant_id="tenant-b") is None
    assert get_pending(conn, pid_b, tenant_id="tenant-a") is None
    assert get_pending(conn, pid_a, tenant_id="tenant-a") is not None

    mark_decided(conn, pid_a, status="approved", resolved_by="someone", tenant_id="tenant-b")
    row = conn.execute(
        "SELECT status FROM pending_decisions WHERE pending_id = ?", (pid_a,)
    ).fetchone()
    assert row[0] == "pending"

    list_none = list_pending(conn, tenant_id=None)
    assert {pid_a, pid_b} <= {p.pending_id for p in list_none}


# ---------------------------------------------------------------------------
# 8. Ordering
# ---------------------------------------------------------------------------


def test_list_pending_ordering_and_paging(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_0001")
    scores = [0.75, 0.90, 0.75, 0.80]
    pending_ids = []
    for i, score in enumerate(scores):
        entity = _make_entity(f"Entity {i}", source_id=f"SRC-{i}")
        disp = _disposition(score=score, source_entity_id=f"src-{i}")
        pid = enqueue_pending(conn, disp, entity, _default_proposal())
        pending_ids.append(pid)

    all_rows = list_pending(conn, limit=100, offset=0)
    row_scores = [(p.top_score, p.pending_id) for p in all_rows]
    assert row_scores == sorted(row_scores, key=lambda t: (-t[0], t[1]))

    paged = list_pending(conn, limit=2, offset=1)
    assert len(paged) == 2
    assert [p.pending_id for p in paged] == [p.pending_id for p in all_rows[1:3]]


# ---------------------------------------------------------------------------
# 9. Transaction neutrality
# ---------------------------------------------------------------------------


def test_no_commit_or_rollback_in_module() -> None:
    text = PENDING_STORE_PATH.read_text()
    matches = re.findall(r"\.commit\(|\.rollback\(", text)
    assert matches == []


def test_enqueue_respects_callers_rollback(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_0001")
    entity = _make_entity("Meridian Consulting Group")
    before = conn.execute("SELECT COUNT(*) FROM pending_decisions").fetchone()[0]

    disp = _disposition()
    pid = enqueue_pending(conn, disp, entity, _default_proposal())
    assert pid is not None
    conn.rollback()

    after = conn.execute("SELECT COUNT(*) FROM pending_decisions").fetchone()[0]
    assert after == before


# ---------------------------------------------------------------------------
# 10. Privacy containment
# ---------------------------------------------------------------------------


def test_privacy_containment_grep_both_directions() -> None:
    assert "llm_training_data" not in PENDING_STORE_PATH.read_text()
    assert "pending_decisions" not in TRAINING_DATA_PATH.read_text()


def test_enqueue_does_not_touch_llm_training_data(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_0001")
    entity = _make_entity("Meridian Consulting Group")
    before = conn.execute("SELECT COUNT(*) FROM llm_training_data").fetchone()[0]

    disp = _disposition(llm_assessment=_llm_assessment())
    enqueue_pending(conn, disp, entity, _default_proposal())

    after = conn.execute("SELECT COUNT(*) FROM llm_training_data").fetchone()[0]
    assert after == before


def test_privacy_stance_raw_name_and_email_persisted(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_0001")
    entity = _make_entity(
        "Chen, Michael",
        entity_category="person",
        source_id="PERSON-1",
        email="michael.chen@example.com",
    )
    disp = _disposition()
    pid = enqueue_pending(conn, disp, entity, _default_proposal())

    row = conn.execute(
        "SELECT entity_json FROM pending_decisions WHERE pending_id = ?", (pid,)
    ).fetchone()
    entity_json_text = row[0]
    assert "Chen, Michael" in entity_json_text or entity.raw_name in entity_json_text
    assert "michael.chen@example.com" in entity_json_text

    parsed = json.loads(entity_json_text)
    assert parsed["email"] == "michael.chen@example.com"
    assert parsed["raw_name"] == entity.raw_name

    assert "core.matching.redaction" not in PENDING_STORE_PATH.read_text()
    assert "from core.matching import redaction" not in PENDING_STORE_PATH.read_text()


# ---------------------------------------------------------------------------
# 11. No Postgres / driver / requirements drift
# ---------------------------------------------------------------------------


def test_no_postgres_or_forbidden_references() -> None:
    pattern = re.compile(r"psycopg|DATABASE_URL|postgres|approval_decisions|audit_log", re.IGNORECASE)
    for path in (PENDING_STORE_PATH, PENDING_MIGRATION):
        text = path.read_text()
        assert not pattern.search(text), f"forbidden reference found in {path}"


