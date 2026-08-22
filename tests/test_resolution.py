"""Tests for Pipeline Stage 6 (`core.graph.resolution` + `core.matching.training_data`).

Pure SQLite, no database server, no network. Schema is loaded from files
(`db/schema_sqlite.sql` + `db/migrations/002_llm_training_data_sqlite.sql`),
never from Python DDL — following the `SQLITE_SCHEMA` / `TRAINING_MIGRATION`
constants pattern in `tests/test_llm_fallback.py`.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import pathlib
import re
import sqlite3
import subprocess
from typing import Any, Optional

import pytest

import core.graph.entity_store as entity_store
from core.graph.resolution import (
    create_new_entity,
    mark_indices_stale,
    reject_match,
    reset_indices_stale_flag,
    resolve_match,
)
from core.ingestion.normalizer import normalize_entity
from core.matching.indices import TokenIndex
from core.matching.llm_fallback import llm_assess, reset_call_budget
from core.matching.redaction import leak_check
from core.matching.training_data import TrainingPair, store_training_pair
from core.matching.types import (
    Disposition,
    GraphEvidence,
    LLMAssessment,
    ScoredMatch,
    SignalBreakdown,
)


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQLITE_SCHEMA = REPO_ROOT / "db" / "schema_sqlite.sql"
TRAINING_MIGRATION = REPO_ROOT / "db" / "migrations" / "002_llm_training_data_sqlite.sql"
ENTITY_STORE_PATH = REPO_ROOT / "core" / "graph" / "entity_store.py"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SQLITE_SCHEMA.read_text())
    c.executescript(TRAINING_MIGRATION.read_text())
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Module-level globals (staleness bit, LLM call budget) must not
    bleed across tests."""
    reset_indices_stale_flag()
    reset_call_budget()
    yield
    reset_indices_stale_flag()
    reset_call_budget()


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
            alias_boost_fired=False,
            abbreviation_bonus_fired=False,
        ),
        graph_evidence=GraphEvidence(
            shared_person_count=0,
            shared_person_bonus=0.0,
            neighborhood_overlap_count=0,
            neighborhood_overlap_bonus=0.0,
        ),
        category_pair=category_pair,
        weight_profile_id="default_v1",
    )


def _disposition(
    canonical_id: str,
    score: float = 0.60,
    action: str = "LLM_FALLBACK",
    llm_assessment: Optional[LLMAssessment] = None,
    tenant_id: Optional[str] = None,
) -> Disposition:
    top = _make_scored_match(canonical_id, score)
    return Disposition(
        source_entity_id="src-1",
        action=action,
        top_match=top,
        candidates_ranked=(top,),
        cluster_conflict=False,
        llm_assessment=llm_assessment,
        tenant_id=tenant_id,
    )


class FakeLLMClient:
    """Minimal `LLMClient` Protocol implementation (mirrors
    `tests/test_llm_fallback.py`)."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response

    def assess(self, system_prompt: str, user_prompt: str, tool_spec: dict[str, Any]) -> dict[str, Any]:
        return dict(self._response)


def _run_stage5(
    conn: sqlite3.Connection,
    canonical_id: str,
    entity,
    score: float = 0.60,
) -> Disposition:
    """Drive a real Stage 5 call so a genuine `llm_training_data` row
    exists, returning the resulting QUEUE_FOR_REVIEW Disposition whose
    `llm_assessment.call_id` references that row."""
    disp = _disposition(canonical_id, score=score, action="LLM_FALLBACK")
    client = FakeLLMClient(
        {"match": True, "confidence": 0.65, "reasoning": "similar tokens", "signals": ["category"]}
    )
    return llm_assess(disp, entity, conn, client=client)


# ---------------------------------------------------------------------------
# 1. Import surface
# ---------------------------------------------------------------------------


def test_stage6_public_functions_import() -> None:
    from core.graph.resolution import create_new_entity, reject_match, resolve_match  # noqa: F401


# ---------------------------------------------------------------------------
# 2. Read-signature preservation (derived, not enumerated)
# ---------------------------------------------------------------------------


def test_entity_store_read_signatures_preserved_from_head() -> None:
    result = subprocess.run(
        ["git", "show", "HEAD:core/graph/entity_store.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"git show HEAD:core/graph/entity_store.py failed: {result.stderr}"
    baseline_source = result.stdout
    tree = ast.parse(baseline_source)

    baseline_funcs: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            args_str = ast.unparse(node.args)
            baseline_funcs[node.name] = f"({args_str})"

    assert baseline_funcs, "expected at least one public function in the HEAD baseline"

    for name, baseline_sig in baseline_funcs.items():
        fn = getattr(entity_store, name, None)
        assert fn is not None, f"{name} missing from current core.graph.entity_store"
        current_sig = str(inspect.signature(fn))
        # Compare via inspect.signature on BOTH sides for an apples-to-apples
        # string, rather than mixing ast.unparse and inspect renderings.
        current_module_ast = ast.parse(inspect.getsource(entity_store))
        current_node = next(
            n
            for n in current_module_ast.body
            if isinstance(n, ast.FunctionDef) and n.name == name
        )
        current_args_str = f"({ast.unparse(current_node.args)})"
        assert current_args_str == baseline_sig, (
            f"{name} signature changed: baseline={baseline_sig!r} current={current_args_str!r}"
        )
        # Also assert it is genuinely still callable/importable with the
        # same name (covers rename/removal even if ast comparison above
        # were somehow skipped).
        assert callable(fn)


# ---------------------------------------------------------------------------
# 3. resolve_match — alias + edge writes, idempotency, atomicity
# ---------------------------------------------------------------------------


def test_resolve_match_creates_alias_and_edge(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_A", "pacrim technologies international")
    _insert_canonical(conn, "CLIENT_B", "pacrim other node")
    entity = _make_entity("PacRim Tech")
    disp = _disposition("CLIENT_A", score=0.60)

    out_cid = resolve_match(
        conn,
        disp,
        entity,
        canonical_id="CLIENT_A",
        alias_confidence=0.85,
        source_node="CLIENT_B",
        target_node="CLIENT_A",
        relationship="SAME_AS",
        source_category="psa",
        target_category="accounting",
        weight=0.85,
        approved_by="user_1",
        tenant_id=None,
    )
    assert out_cid == "CLIENT_A"

    alias_rows = conn.execute(
        "SELECT canonical_id, value, source FROM entity_aliases WHERE canonical_id = ? AND value = ? AND source = ?",
        ("CLIENT_A", entity.normalized_name, entity.source),
    ).fetchall()
    assert len(alias_rows) == 1

    edge_rows = conn.execute(
        """
        SELECT source_category, target_category, weight, approved_by
          FROM entity_edges
         WHERE source_node = ? AND target_node = ? AND relationship = ?
        """,
        ("CLIENT_B", "CLIENT_A", "SAME_AS"),
    ).fetchall()
    assert len(edge_rows) == 1
    src_cat, tgt_cat, weight, approved_by = edge_rows[0]
    assert src_cat == "psa" and tgt_cat == "accounting"
    assert weight == 0.85
    assert approved_by == "user_1"
    assert src_cat is not None and tgt_cat is not None and weight is not None and approved_by is not None


def test_resolve_match_alias_idempotent(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_A", "pacrim technologies international")
    _insert_canonical(conn, "CLIENT_B", "pacrim other node")
    entity = _make_entity("PacRim Tech")
    disp = _disposition("CLIENT_A", score=0.60)

    kwargs = dict(
        canonical_id="CLIENT_A",
        alias_confidence=0.85,
        source_node="CLIENT_B",
        target_node="CLIENT_A",
        relationship="SAME_AS",
        source_category="psa",
        target_category="accounting",
        weight=0.85,
        approved_by="user_1",
        tenant_id=None,
    )
    cid1 = resolve_match(conn, disp, entity, **kwargs)
    count1 = conn.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0]
    cid2 = resolve_match(conn, disp, entity, **kwargs)
    count2 = conn.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0]

    assert cid1 == cid2 == "CLIENT_A"
    assert count1 == count2


def test_resolve_match_edge_idempotent_increments_approval_count(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_A", "pacrim technologies international")
    _insert_canonical(conn, "CLIENT_B", "pacrim other node")
    entity = _make_entity("PacRim Tech")
    disp = _disposition("CLIENT_A", score=0.60)

    kwargs = dict(
        canonical_id="CLIENT_A",
        alias_confidence=0.85,
        source_node="CLIENT_B",
        target_node="CLIENT_A",
        relationship="SAME_AS",
        source_category="psa",
        target_category="accounting",
        weight=0.85,
        approved_by="user_1",
        tenant_id=None,
    )
    resolve_match(conn, disp, entity, **kwargs)
    edge_count_1 = conn.execute("SELECT COUNT(*) FROM entity_edges").fetchone()[0]
    approval_count_1 = conn.execute(
        "SELECT approval_count FROM entity_edges WHERE source_node = ? AND target_node = ?",
        ("CLIENT_B", "CLIENT_A"),
    ).fetchone()[0]

    resolve_match(conn, disp, entity, **kwargs)
    edge_count_2 = conn.execute("SELECT COUNT(*) FROM entity_edges").fetchone()[0]
    approval_count_2 = conn.execute(
        "SELECT approval_count FROM entity_edges WHERE source_node = ? AND target_node = ?",
        ("CLIENT_B", "CLIENT_A"),
    ).fetchone()[0]

    assert edge_count_1 == edge_count_2
    assert approval_count_2 == approval_count_1 + 1


def test_resolve_match_atomicity_on_exception(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    import core.graph.resolution as resolution_mod

    _insert_canonical(conn, "CLIENT_A", "pacrim technologies international")
    _insert_canonical(conn, "CLIENT_B", "pacrim other node")
    entity = _make_entity("PacRim Tech")
    disp = _disposition("CLIENT_A", score=0.60)

    alias_before = conn.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0]
    edge_before = conn.execute("SELECT COUNT(*) FROM entity_edges").fetchone()[0]
    training_before = conn.execute("SELECT COUNT(*) FROM llm_training_data").fetchone()[0]

    def _boom(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("simulated failure after alias insert")

    monkeypatch.setattr(resolution_mod, "upsert_edge", _boom)

    with pytest.raises(RuntimeError):
        resolve_match(
            conn,
            disp,
            entity,
            canonical_id="CLIENT_A",
            alias_confidence=0.85,
            source_node="CLIENT_B",
            target_node="CLIENT_A",
            relationship="SAME_AS",
            source_category="psa",
            target_category="accounting",
            weight=0.85,
            approved_by="user_1",
            tenant_id=None,
        )

    alias_after = conn.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0]
    edge_after = conn.execute("SELECT COUNT(*) FROM entity_edges").fetchone()[0]
    training_after = conn.execute("SELECT COUNT(*) FROM llm_training_data").fetchone()[0]

    assert alias_after == alias_before
    assert edge_after == edge_before
    assert training_after == training_before


def _contains_commit_or_rollback_call(src: str) -> bool:
    """AST-level check for an actual `<expr>.commit(...)` /
    `<expr>.rollback(...)` call — ignores docstring/comment mentions of
    the same substring."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("commit", "rollback")
        ):
            return True
    return False


def test_no_commit_or_rollback_outside_resolution_module() -> None:
    """AST assertion: only `core/graph/resolution.py` may commit/rollback."""
    training_data_src = (REPO_ROOT / "core" / "matching" / "training_data.py").read_text()
    entity_store_src = ENTITY_STORE_PATH.read_text()

    assert not _contains_commit_or_rollback_call(training_data_src)

    # entity_store.py's write functions (everything after the Stage 6 marker
    # comment) must not commit/rollback either — the read functions above it
    # never did.
    marker = "# Stage 6 writes (resolution / graph update)"
    assert marker in entity_store_src
    write_section = entity_store_src.split(marker, 1)[1]
    assert not _contains_commit_or_rollback_call(write_section)


# ---------------------------------------------------------------------------
# 4. create_new_entity
# ---------------------------------------------------------------------------


def test_create_new_entity_creates_canonical_and_system_refs(conn: sqlite3.Connection) -> None:
    entity = _make_entity("Brand New Co", source="quickbooks", entity_category="organization")
    disp = _disposition("CLIENT_NOPE", score=0.55, action="LLM_FALLBACK")
    # candidate must exist for graph_evidence lookups downstream, though
    # create_new_entity never mutates it.
    _insert_canonical(conn, "CLIENT_NOPE", "unrelated company")

    canonical_id = create_new_entity(
        conn,
        disp,
        entity,
        canonical_name="Brand New Co",
        entity_type="client",
        entity_category="organization",
        confidence=0.93,
        system_refs=(
            {
                "source": "quickbooks",
                "category": "accounting",
                "external_id": "QB-999",
                "external_fields": {"DisplayName": "Brand New Co"},
            },
        ),
        approved_by="user_2",
        tenant_id=None,
    )

    assert isinstance(canonical_id, str) and canonical_id

    row = conn.execute(
        "SELECT entity_type, entity_category FROM canonical_entities WHERE canonical_id = ?",
        (canonical_id,),
    ).fetchone()
    assert row is not None
    entity_type, entity_category = row
    assert entity_type in (
        "client",
        "vendor",
        "project",
        "pl_unit",
        "cost_center",
        "contract",
        "person",
    )
    assert entity_category in ("organization", "person")

    ref_rows = conn.execute(
        "SELECT source, external_id FROM system_references WHERE canonical_id = ?",
        (canonical_id,),
    ).fetchall()
    assert len(ref_rows) == 1
    assert ref_rows[0] == ("quickbooks", "QB-999")


# ---------------------------------------------------------------------------
# 5. Index-rebuild visibility + staleness signal
# ---------------------------------------------------------------------------


def test_resolve_match_index_rebuild_visibility(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_XXXX", "pacific rim technologies international")
    _insert_canonical(conn, "CLIENT_OTHER", "some other node")
    entity = _make_entity("PacRim Tech")
    assert entity.normalized_name == "pacrim tech"
    disp = _disposition("CLIENT_XXXX", score=0.60)

    resolve_match(
        conn,
        disp,
        entity,
        canonical_id="CLIENT_XXXX",
        alias_confidence=0.85,
        source_node="CLIENT_OTHER",
        target_node="CLIENT_XXXX",
        relationship="SAME_AS",
        source_category="psa",
        target_category="accounting",
        weight=0.85,
        approved_by="user_1",
        tenant_id=None,
    )

    idx = TokenIndex.build(conn)
    hits = idx.lookup(entity.normalized_name.split())
    assert "CLIENT_XXXX" in hits


def test_mark_indices_stale_signal(conn: sqlite3.Connection) -> None:
    assert mark_indices_stale() is False

    _insert_canonical(conn, "CLIENT_A", "pacrim technologies international")
    _insert_canonical(conn, "CLIENT_B", "pacrim other node")
    entity = _make_entity("PacRim Tech")
    disp = _disposition("CLIENT_A", score=0.60)
    resolve_match(
        conn,
        disp,
        entity,
        canonical_id="CLIENT_A",
        alias_confidence=0.85,
        source_node="CLIENT_B",
        target_node="CLIENT_A",
        relationship="SAME_AS",
        source_category="psa",
        target_category="accounting",
        weight=0.85,
        approved_by="user_1",
        tenant_id=None,
    )
    assert mark_indices_stale() is True


def test_mark_indices_stale_false_after_reject_match_alone(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_A", "pacrim technologies international")
    entity = _make_entity("PacRim Tech")
    disp = _disposition("CLIENT_A", score=0.55)

    assert mark_indices_stale() is False
    reject_match(conn, disp, entity, rejected_canonical_id="CLIENT_A", tenant_id=None)
    assert mark_indices_stale() is False


# ---------------------------------------------------------------------------
# 6. TrainingPair shape
# ---------------------------------------------------------------------------


def test_training_pair_has_exactly_the_specified_fields() -> None:
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(TrainingPair)}
    assert field_names == {
        "entity_pair",
        "signal_breakdown",
        "graph_evidence",
        "category_pair",
        "disposition",
        "reasoning_trace",
    }


# ---------------------------------------------------------------------------
# 7. store_training_pair — LLM gating
# ---------------------------------------------------------------------------


def test_store_training_pair_llm_gated_capture(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_A", "pacrim technologies international")
    entity = _make_entity("PacRim Tech")
    stage5_disp = _run_stage5(conn, "CLIENT_A", entity, score=0.60)
    assert stage5_disp.llm_assessment is not None

    pair = TrainingPair(
        entity_pair={
            "canonical_id": "CLIENT_A",
            "incoming_entity_raw": entity.raw_name,
            "source_call_id": stage5_disp.llm_assessment.call_id,
        },
        signal_breakdown={"token_set_ratio": 88.0},
        graph_evidence={"shared_person_count": 0},
        category_pair="psa:accounting",
        disposition="CONFIRMED",
        reasoning_trace="matched on token overlap",
    )
    call_id = store_training_pair(conn, pair, tenant_id=None)
    conn.commit()

    assert call_id is not None
    assert call_id.startswith("resolution:")
    assert re.match(r"^[a-z_]+:[a-z_]+$", pair.category_pair)

    row = conn.execute(
        "SELECT category_pair, redacted_prompt, prompt_sha256 FROM llm_training_data WHERE call_id = ?",
        (call_id,),
    ).fetchone()
    assert row is not None
    category_pair, redacted_prompt, prompt_sha256 = row
    assert category_pair == "psa:accounting"

    stage5_row = conn.execute(
        "SELECT redacted_prompt, prompt_sha256 FROM llm_training_data WHERE call_id = ?",
        (stage5_disp.llm_assessment.call_id,),
    ).fetchone()
    assert redacted_prompt == stage5_row[0]
    assert prompt_sha256 == stage5_row[1]
    assert hashlib.sha256(redacted_prompt.encode("utf-8")).hexdigest() == prompt_sha256


def test_store_training_pair_no_llm_prompt_returns_none(conn: sqlite3.Connection) -> None:
    pair = TrainingPair(
        entity_pair={"canonical_id": "CLIENT_A", "incoming_entity_raw": "Acme"},
        signal_breakdown={},
        graph_evidence={},
        category_pair="psa:accounting",
        disposition="CONFIRMED",
        reasoning_trace="",
    )
    before = conn.execute("SELECT COUNT(*) FROM llm_training_data").fetchone()[0]
    result = store_training_pair(conn, pair, tenant_id=None)
    after = conn.execute("SELECT COUNT(*) FROM llm_training_data").fetchone()[0]
    assert result is None
    assert before == after


def test_store_training_pair_dangling_call_id_returns_none(conn: sqlite3.Connection) -> None:
    pair = TrainingPair(
        entity_pair={
            "canonical_id": "CLIENT_A",
            "incoming_entity_raw": "Acme",
            "source_call_id": "does-not-exist",
        },
        signal_breakdown={},
        graph_evidence={},
        category_pair="psa:accounting",
        disposition="CONFIRMED",
        reasoning_trace="",
    )
    before = conn.execute("SELECT COUNT(*) FROM llm_training_data").fetchone()[0]
    result = store_training_pair(conn, pair, tenant_id=None)
    after = conn.execute("SELECT COUNT(*) FROM llm_training_data").fetchone()[0]
    assert result is None
    assert before == after


def test_llm_response_json_shape_and_no_entity_pair_key(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_A", "pacrim technologies international")
    entity = _make_entity("PacRim Tech")
    stage5_disp = _run_stage5(conn, "CLIENT_A", entity, score=0.60)

    pair = TrainingPair(
        entity_pair={
            "canonical_id": "CLIENT_A",
            "incoming_entity_raw": entity.raw_name,
            "source_call_id": stage5_disp.llm_assessment.call_id,
        },
        signal_breakdown={"token_set_ratio": 88.0},
        graph_evidence={"shared_person_count": 0},
        category_pair="psa:accounting",
        disposition="CONFIRMED",
        reasoning_trace="matched on token overlap",
    )
    call_id = store_training_pair(conn, pair, tenant_id=None)
    conn.commit()

    row = conn.execute(
        "SELECT llm_response_json FROM llm_training_data WHERE call_id = ?", (call_id,)
    ).fetchone()
    payload = json.loads(row[0])
    assert set(payload.keys()) == {
        "signal_breakdown",
        "graph_evidence",
        "category_pair",
        "disposition",
        "reasoning_trace",
        "entity_pair_ref",
    }
    assert "entity_pair" not in payload
    assert payload["entity_pair_ref"] == {
        "canonical_id": "CLIENT_A",
        "source_call_id": stage5_disp.llm_assessment.call_id,
    }


# ---------------------------------------------------------------------------
# 8. reject_match
# ---------------------------------------------------------------------------


def test_reject_match_through_llm_produces_rejected_row(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_A", "pacrim technologies international")
    entity = _make_entity("PacRim Tech")
    stage5_disp = _run_stage5(conn, "CLIENT_A", entity, score=0.55)

    call_id = reject_match(
        conn, stage5_disp, entity, rejected_canonical_id="CLIENT_A", reasoning_trace="not a match", tenant_id=None
    )
    conn.commit()

    assert call_id is not None
    row = conn.execute(
        "SELECT llm_response_json FROM llm_training_data WHERE call_id = ?", (call_id,)
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["disposition"] == "REJECTED"
    assert payload["signal_breakdown"]


def test_reject_match_no_graph_mutation(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_A", "pacrim technologies international")
    entity = _make_entity("PacRim Tech")
    disp = _disposition("CLIENT_A", score=0.55)

    alias_before = conn.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0]
    edge_before = conn.execute("SELECT COUNT(*) FROM entity_edges").fetchone()[0]
    canonical_before = conn.execute("SELECT COUNT(*) FROM canonical_entities").fetchone()[0]

    reject_match(conn, disp, entity, rejected_canonical_id="CLIENT_A", tenant_id=None)

    assert conn.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0] == alias_before
    assert conn.execute("SELECT COUNT(*) FROM entity_edges").fetchone()[0] == edge_before
    assert conn.execute("SELECT COUNT(*) FROM canonical_entities").fetchone()[0] == canonical_before


# ---------------------------------------------------------------------------
# 9. Privacy — leak checks
# ---------------------------------------------------------------------------


def test_privacy_covers_both_persisted_columns_for_person_pair(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "PERSON_A", "neal iyer", entity_type="person", entity_category="person")
    entity = _make_entity(
        "Neal Iyer",
        source="ruddr",
        entity_category="person",
        email="neal.iyer@example.com",
        employee_id="EMP-4471",
    )
    disp = _disposition("PERSON_A", score=0.55, action="LLM_FALLBACK")
    client = FakeLLMClient(
        {"match": True, "confidence": 0.6, "reasoning": "role and token overlap match", "signals": ["role"]}
    )
    stage5_disp = llm_assess(disp, entity, conn, client=client)

    call_id = reject_match(
        conn, stage5_disp, entity, rejected_canonical_id="PERSON_A", reasoning_trace="reviewed and declined", tenant_id=None
    )
    conn.commit()
    assert call_id is not None

    row = conn.execute(
        "SELECT redacted_prompt, llm_response_json FROM llm_training_data WHERE call_id = ?",
        (call_id,),
    ).fetchone()
    redacted_prompt, llm_response_json = row

    forbidden = frozenset(
        {"neal iyer", "neal.iyer@example.com", "emp-4471", "neal", "iyer"}
    )
    for needle in ("neal iyer", "neal.iyer@example.com", "emp-4471"):
        assert needle not in redacted_prompt.lower()
        assert needle not in llm_response_json.lower()

    assert leak_check(redacted_prompt, forbidden) is None
    assert leak_check(llm_response_json, forbidden) is None


def test_leak_aborts_the_write(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CLIENT_A", "pacrim technologies international")
    entity = _make_entity("PacRim Tech")
    stage5_disp = _run_stage5(conn, "CLIENT_A", entity, score=0.60)

    before = conn.execute("SELECT COUNT(*) FROM llm_training_data").fetchone()[0]

    leaking_pair = TrainingPair(
        entity_pair={
            "canonical_id": "CLIENT_A",
            "incoming_entity_raw": entity.raw_name,
            "source_call_id": stage5_disp.llm_assessment.call_id,
            "incoming_entity_normalized": entity.normalized_name,
        },
        signal_breakdown={"token_set_ratio": 88.0},
        graph_evidence={"shared_person_count": 0},
        category_pair="psa:accounting",
        disposition="CONFIRMED",
        # Forces a forbidden token (the entity's own normalized name) into
        # the persisted payload via reasoning_trace.
        reasoning_trace=f"forced leak of {entity.normalized_name}",
    )

    with pytest.raises(ValueError):
        store_training_pair(conn, leaking_pair, tenant_id=None)

    after = conn.execute("SELECT COUNT(*) FROM llm_training_data").fetchone()[0]
    assert after == before


# ---------------------------------------------------------------------------
# 10. Append-only / no-Postgres / no-transactions grep assertions
# ---------------------------------------------------------------------------


def test_training_capture_is_append_only_in_code() -> None:
    for path in (
        REPO_ROOT / "core" / "matching" / "training_data.py",
        REPO_ROOT / "core" / "graph" / "resolution.py",
    ):
        src = path.read_text()
        assert "UPDATE llm_training_data" not in src
        assert "DELETE FROM llm_training_data" not in src


def test_no_postgres_references() -> None:
    pattern = re.compile(r"psycopg|DATABASE_URL|postgres", re.IGNORECASE)
    for path in (
        REPO_ROOT / "core" / "graph" / "resolution.py",
        REPO_ROOT / "core" / "matching" / "training_data.py",
        ENTITY_STORE_PATH,
    ):
        src = path.read_text()
        assert not pattern.search(src), f"{path} references Postgres infrastructure"


def test_requirements_txt_unchanged() -> None:
    result = subprocess.run(
        ["git", "diff", "--", "requirements.txt"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_no_transactions_table_reference() -> None:
    for path in (
        REPO_ROOT / "core" / "graph" / "resolution.py",
        REPO_ROOT / "core" / "matching" / "training_data.py",
    ):
        src = path.read_text()
        assert "transactions" not in src
