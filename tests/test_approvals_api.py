"""Tests for the approvals API router and the approval-queue dashboard
page (feature 11).

Pure SQLite, no database server, no network. `NEXUS_STORE_PATH` points
every test at a per-test temporary file whose schema is built from
`db/schema_sqlite.sql` + the training migration + the pending-decisions
migration (located by glob, never by number) — the same pattern
`tests/test_pending_decisions.py` uses. Never `CREATE TABLE` inside a
test.

`TestClient(app)` is used WITHOUT the `with` context manager throughout
this module, so the app's `startup` lifespan hook (which would open a
live database connection when one is configured) never runs — the
build hazard this feature's brief calls out.
"""

from __future__ import annotations

import inspect
import pathlib
import re
import sqlite3
import subprocess
import sys
import types
import uuid
from typing import Any, Optional
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import api.routers.approvals as approvals_mod
import core.graph.resolution as resolution_mod
import core.matching.pending_store as pending_store_mod
from api.main import app
from api.middleware.tenant import DEFAULT_TENANT_ID
from core.graph.resolution import reject_match, resolve_match
from core.ingestion.normalizer import normalize_entity
from core.matching.engine import _build_confirmed_proposal
from core.matching.pending_store import enqueue_pending, get_pending, rehydrate
from core.matching.scoring import BoostEntry
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
PENDING_MIGRATION_CANDIDATES = sorted(
    REPO_ROOT.glob("db/migrations/*_pending_decisions_sqlite.sql")
)
assert PENDING_MIGRATION_CANDIDATES, "no *_pending_decisions_sqlite.sql migration found"
PENDING_MIGRATION = PENDING_MIGRATION_CANDIDATES[0]

APPROVALS_ROUTER_PATH = REPO_ROOT / "api" / "routers" / "approvals.py"
APPROVAL_QUEUE_PAGE_PATH = REPO_ROOT / "dashboard" / "pages" / "approval_queue.py"


# ---------------------------------------------------------------------------
# Fixtures + shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def store_path(tmp_path, monkeypatch):
    path = tmp_path / "store.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SQLITE_SCHEMA.read_text())
        conn.executescript(TRAINING_MIGRATION.read_text())
        conn.executescript(PENDING_MIGRATION.read_text())
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("NEXUS_STORE_PATH", str(path))
    return path


@pytest.fixture()
def client(store_path):
    return TestClient(app)


def _insert_canonical(
    path,
    canonical_id: str,
    tenant_id: Optional[str],
    canonical_name: str = "acme",
) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            INSERT INTO canonical_entities (
                canonical_id, tenant_id, canonical_name, entity_type, entity_category, confidence
            ) VALUES (?, ?, ?, 'client', 'organization', 0.95)
            """,
            (canonical_id, tenant_id, canonical_name),
        )
        conn.commit()
    finally:
        conn.close()


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
    category_pair: tuple = ("psa", "accounting"),
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
                (BoostEntry(signal_id="B1", raw=0.05, applied=0.05),) if with_boosts else ()
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


def _seed_pending(
    path,
    tenant_id: Optional[str],
    canonical_id: str,
    canonical_name: str = "acme",
    score: float = 0.80,
    source_entity_id: str = "src-1",
    source_id: str = "RUDDR-X",
    display_name: str = "Acme Consulting",
    llm_assessment: Optional[LLMAssessment] = None,
    with_boosts: bool = False,
) -> str:
    """Seed one canonical entity + one `QUEUE_FOR_REVIEW` pending row via
    10b's real `enqueue_pending`, using the same proposal shape the
    pipeline (`core.matching.engine._build_confirmed_proposal`) builds."""
    _insert_canonical(path, canonical_id, tenant_id, canonical_name)
    entity = _make_entity(display_name, source_id=source_id)
    top = _make_scored_match(canonical_id, score, with_boosts=with_boosts)
    disposition = Disposition(
        source_entity_id=source_entity_id,
        action="QUEUE_FOR_REVIEW",
        top_match=top,
        candidates_ranked=(top,),
        cluster_conflict=False,
        llm_assessment=llm_assessment,
        tenant_id=tenant_id,
    )
    proposal = _build_confirmed_proposal(entity, canonical_id, score, "seeded")

    conn = sqlite3.connect(path)
    try:
        pending_id = enqueue_pending(conn, disposition, entity, proposal, tenant_id=tenant_id)
        conn.commit()
    finally:
        conn.close()
    assert pending_id is not None
    return pending_id


def _row_state(path, pending_id: str) -> tuple:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT status, resolved_by, resolved_at, outcome_canonical_id "
            "FROM pending_decisions WHERE pending_id = ?",
            (pending_id,),
        ).fetchone()
    finally:
        conn.close()
    return row


def _route_pairs(routes) -> set:
    pairs = set()
    for route in routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or path is None:
            continue
        for method in methods:
            if method == "HEAD":
                continue
            pairs.add((method, path))
    return pairs


# ---------------------------------------------------------------------------
# 1-3. Router shape / reachability / additive registration
# ---------------------------------------------------------------------------

_EXPECTED_ROUTE_PAIRS = {
    ("GET", "/approvals/pending"),
    ("GET", "/approvals/{pending_id}"),
    ("POST", "/approvals/{pending_id}/approve"),
    ("POST", "/approvals/{pending_id}/reject"),
    ("POST", "/approvals/{pending_id}/correct"),
}


def test_router_routes_are_exactly_the_spec_set():
    assert _EXPECTED_ROUTE_PAIRS
    assert _route_pairs(approvals_mod.router.routes) == _EXPECTED_ROUTE_PAIRS


def test_router_reachable_from_app():
    router_pairs = _route_pairs(approvals_mod.router.routes)
    assert router_pairs
    app_pairs = _route_pairs(app.routes)
    assert router_pairs <= app_pairs


def _load_pre_registration_main_module() -> types.ModuleType:
    """Exec the last `api/main.py` revision in git history that predates
    the `approvals.router` registration, so 'before' is derived from git
    history rather than transcribed into this test. Walking history (instead
    of assuming `HEAD`) keeps this correct even once the registration itself
    is committed at `HEAD`."""
    log = subprocess.run(
        ["git", "log", "--format=%H", "--follow", "--", "api/main.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    for sha in log:
        content = subprocess.run(
            ["git", "show", f"{sha}:api/main.py"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        if "approvals.router" not in content:
            module = types.ModuleType("_pre_registration_api_main_test11")
            module.__file__ = str(REPO_ROOT / "api/main.py")
            sys.modules[module.__name__] = module
            try:
                exec(compile(content, module.__file__, "exec"), module.__dict__)
            finally:
                sys.modules.pop(module.__name__, None)
            return module
    raise AssertionError("no revision of api/main.py predates approvals.router registration")


def test_registration_is_additive_and_middleware_unchanged():
    head_main = _load_pre_registration_main_module()
    before_pairs = _route_pairs(head_main.app.routes)
    assert before_pairs
    after_pairs = _route_pairs(app.routes)
    assert before_pairs < after_pairs

    before_mw = [m.cls for m in head_main.app.user_middleware]
    after_mw = [m.cls for m in app.user_middleware]
    assert before_mw == after_mw


# ---------------------------------------------------------------------------
# 4-5. Tenant scoping via middleware only
# ---------------------------------------------------------------------------


def test_pending_list_scoped_per_tenant_with_ordering_tiebreak(client, store_path):
    tenant_a = str(uuid.uuid4())
    tenant_b = str(uuid.uuid4())

    _seed_pending(store_path, tenant_a, "CLIENT_A1", score=0.75, source_entity_id="a-1", source_id="A-1")
    _seed_pending(store_path, tenant_a, "CLIENT_A2", score=0.90, source_entity_id="a-2", source_id="A-2")
    _seed_pending(store_path, tenant_a, "CLIENT_A3", score=0.75, source_entity_id="a-3", source_id="A-3")
    _seed_pending(store_path, tenant_b, "CLIENT_B1", score=0.85, source_entity_id="b-1", source_id="B-1")

    resp_a = client.get("/approvals/pending", headers={"X-Nexus-Tenant": tenant_a})
    resp_b = client.get("/approvals/pending", headers={"X-Nexus-Tenant": tenant_b})
    assert resp_a.status_code == 200
    assert resp_b.status_code == 200

    items_a = resp_a.json()
    items_b = resp_b.json()
    assert items_a
    assert items_b

    ids_a = {item["top_canonical_id"] for item in items_a}
    ids_b = {item["top_canonical_id"] for item in items_b}
    assert ids_a == {"CLIENT_A1", "CLIENT_A2", "CLIENT_A3"}
    assert ids_b == {"CLIENT_B1"}

    conn = sqlite3.connect(store_path)
    try:
        from core.matching.pending_store import list_pending

        expected_order = [p.pending_id for p in list_pending(conn, tenant_id=tenant_a)]
    finally:
        conn.close()
    assert [item["pending_id"] for item in items_a] == expected_order
    # A genuine tie was exercised.
    scores = [item["top_score"] for item in items_a]
    assert len(scores) != len(set(scores))


def test_router_reads_tenant_from_middleware_only():
    src = APPROVALS_ROUTER_PATH.read_text()
    assert "request.state.tenant_id" in src
    assert "X-Nexus-Tenant" not in src
    assert "NEXUS_TENANT_ID" not in src
    assert "DEFAULT_TENANT_ID" not in src


def test_header_present_returns_that_tenants_rows(client, store_path):
    tenant_id = str(uuid.uuid4())
    _seed_pending(store_path, tenant_id, "CLIENT_HDR", score=0.8)
    resp = client.get("/approvals/pending", headers={"X-Nexus-Tenant": tenant_id})
    assert resp.status_code == 200
    assert resp.json()


def test_header_absent_falls_back_to_default_tenant(client, store_path, monkeypatch):
    monkeypatch.delenv("NEXUS_TENANT_ID", raising=False)
    _seed_pending(store_path, DEFAULT_TENANT_ID, "CLIENT_DEFAULT", score=0.8)
    resp = client.get("/approvals/pending")
    assert resp.status_code == 200
    assert resp.json()


# ---------------------------------------------------------------------------
# 6. No unscoped call is possible
# ---------------------------------------------------------------------------


def test_no_unscoped_call_possible(client, store_path):
    tenant_id = str(uuid.uuid4())
    pending_id = _seed_pending(store_path, tenant_id, "CLIENT_SPY", score=0.8)

    calls: list = []
    targets = {
        "get_pending": pending_store_mod.get_pending,
        "list_pending": pending_store_mod.list_pending,
        "mark_decided": pending_store_mod.mark_decided,
        "resolve_match": resolution_mod.resolve_match,
        "reject_match": resolution_mod.reject_match,
    }

    def _make_spy(name, original):
        sig = inspect.signature(original)

        def _spy(*args, **kwargs):
            if "tenant_id" in sig.parameters:
                bound = sig.bind_partial(*args, **kwargs)
                bound.apply_defaults()
                calls.append((name, bound.arguments.get("tenant_id")))
            return original(*args, **kwargs)

        _spy.__signature__ = sig
        return _spy

    patched = {name: _make_spy(name, fn) for name, fn in targets.items()}
    with mock.patch.multiple(approvals_mod, **patched):
        client.get("/approvals/pending", headers={"X-Nexus-Tenant": tenant_id})
        client.post(f"/approvals/{pending_id}/approve", headers={"X-Nexus-Tenant": tenant_id})

    assert calls
    assert all(recorded_tenant is not None for _, recorded_tenant in calls)


# ---------------------------------------------------------------------------
# 7. No tenant-isolation language
# ---------------------------------------------------------------------------


def test_no_rls_or_isolation_language_grep():
    # Built from single-character / word fragments so this assertion's
    # own source line is not itself a match for the pattern it checks
    # for (grep would otherwise flag this test file).
    _rls = "R" + "L" + "S"
    _create_policy = "CREATE" + " " + "POLICY"
    _row_level_security = "row" + ".level " + "security"
    pattern = r"\b" + _rls + r"\b|" + _create_policy + "|" + _row_level_security
    result = subprocess.run(
        [
            "grep",
            "-rniE",
            pattern,
            "api/routers/approvals.py",
            "dashboard/pages/approval_queue.py",
            "tests/test_approvals_api.py",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# 8-9. Approve drives Stage 6 for real; argument coverage is derived
# ---------------------------------------------------------------------------


def test_approve_drives_stage6_from_row_alone(client, store_path):
    tenant_id = str(uuid.uuid4())
    pending_id = _seed_pending(
        store_path, tenant_id, "CLIENT_APPROVE", canonical_name="approve co", score=0.8
    )

    resp = client.post(
        f"/approvals/{pending_id}/approve",
        headers={"X-Nexus-Tenant": tenant_id},
        json={"approved_by": "reviewer_1"},
    )
    assert resp.status_code == 200
    canonical_id = resp.json()["canonical_id"]
    assert canonical_id == "CLIENT_APPROVE"

    conn = sqlite3.connect(store_path)
    try:
        alias_row = conn.execute(
            "SELECT canonical_id, value, source, category FROM entity_aliases "
            "WHERE canonical_id = ?",
            ("CLIENT_APPROVE",),
        ).fetchone()
        edge_row = conn.execute(
            "SELECT source_node, target_node, relationship, approval_count, approved_by "
            "FROM entity_edges WHERE source_node = ? AND target_node = ?",
            ("CLIENT_APPROVE", "CLIENT_APPROVE"),
        ).fetchone()
    finally:
        conn.close()

    assert alias_row is not None
    assert alias_row[0] == "CLIENT_APPROVE"
    assert alias_row[2] == "ruddr"
    assert alias_row[3] == "psa"

    assert edge_row is not None
    assert edge_row[2] == "SAME_AS"
    assert edge_row[3] == 1
    assert edge_row[4] == "reviewer_1"

    status, resolved_by, resolved_at, outcome = _row_state(store_path, pending_id)
    assert status == "approved"
    assert resolved_by == "reviewer_1"
    assert resolved_at is not None
    assert outcome == "CLIENT_APPROVE"


def _required_parameters(fn) -> set:
    """Names of `fn`'s parameters that a caller MUST supply."""
    return {
        name
        for name, param in inspect.signature(fn).parameters.items()
        if param.default is inspect.Parameter.empty
        and param.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }


def test_argument_coverage_is_derived_not_enumerated(store_path):
    """Every routed Stage 6 writer must be demonstrably called with all of
    its required arguments, with no argument list transcribed here.

    The router's own decision functions are driven for real; only the
    writer itself is swapped for a recorder that carries the real
    writer's signature (which is what the router's dispatcher introspects
    to decide what to pass). Binding the recorded call against the real
    signature is the coverage proof: a missing required argument raises
    `TypeError` at `bind`. Checked per writer, so a writer whose
    proposal-sourced remainder happens to be empty still has its full
    required set verified — it cannot ride along on another writer.
    """
    tenant_id = str(uuid.uuid4())

    # Each routed writer, paired with the router entry point that routes
    # to it. Argument NAMES are never written down — only these two
    # routing facts, which are what the test is about.
    routes = (
        (resolve_match, "resolve_match", approvals_mod.approve_decision, "CLIENT_COV_APPROVE"),
        (reject_match, "reject_match", approvals_mod.reject_decision, "CLIENT_COV_REJECT"),
    )
    assert routes

    proposal_sourced: set = set()
    for index, (writer, attr_name, decide, canonical_id) in enumerate(routes):
        pending_id = _seed_pending(
            store_path,
            tenant_id,
            canonical_id,
            score=0.8,
            source_entity_id=f"cov-{index}",
            source_id=f"COV-{index}",
        )
        conn = sqlite3.connect(store_path)
        try:
            pending = get_pending(conn, pending_id, tenant_id)
            assert pending is not None
            _disposition, _entity, proposal = rehydrate(pending)
            assert proposal

            signature = inspect.signature(writer)
            required = _required_parameters(writer)
            # Non-emptiness precondition: a writer with no required
            # arguments would make the coverage claim below meaningless.
            assert required, f"{attr_name} has no required arguments to cover"

            recorded: list = []

            def _recorder(*args, **kwargs):
                recorded.append((args, kwargs))
                return canonical_id

            _recorder.__signature__ = signature

            with mock.patch.object(approvals_mod, attr_name, _recorder):
                decide(conn, tenant_id, pending_id)

            assert len(recorded) == 1, f"{attr_name} was not routed to exactly once"
            args, kwargs = recorded[0]
            # Raises TypeError if any required argument was not supplied.
            bound = signature.bind(*args, **kwargs)
            supplied = set(bound.arguments)
            assert required <= supplied, (
                f"{attr_name} called without required arguments: "
                f"{sorted(required - supplied)}"
            )

            for name in required & set(proposal):
                assert bound.arguments[name] == proposal[name], (
                    f"{attr_name} received {name}={bound.arguments[name]!r}, "
                    f"not the rehydrated proposal's {proposal[name]!r}"
                )
            proposal_sourced |= required & set(proposal)
        finally:
            conn.close()

    # At least one required argument really came out of the rehydrated
    # proposal — otherwise the value checks above never ran on anything.
    assert proposal_sourced


# ---------------------------------------------------------------------------
# 10. Reject with unknown candidate -> 400
# ---------------------------------------------------------------------------


def test_reject_unknown_candidate_returns_400_not_500(client, store_path):
    tenant_id = str(uuid.uuid4())
    pending_id = _seed_pending(store_path, tenant_id, "CLIENT_REJ400", score=0.8)

    resp = client.post(
        f"/approvals/{pending_id}/reject",
        headers={"X-Nexus-Tenant": tenant_id},
        json={"rejected_canonical_id": "NOT_A_CANDIDATE"},
    )
    assert resp.status_code == 400

    status, _resolved_by, _resolved_at, _outcome = _row_state(store_path, pending_id)
    assert status == "pending"


# ---------------------------------------------------------------------------
# 11. Reject writes a negative training pair only when Stage 5 fired
# ---------------------------------------------------------------------------


def _seed_stage5_row(path, call_id: str, tenant_id: Optional[str]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            INSERT INTO llm_training_data (
                call_id, tenant_id, category_pair, redacted_prompt, prompt_sha256, llm_response_json
            ) VALUES (?, ?, 'psa:accounting', 'redacted prompt text', 'deadbeef', '{}')
            """,
            (call_id, tenant_id),
        )
        conn.commit()
    finally:
        conn.close()


def _training_row_count(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM llm_training_data").fetchone()[0]
    finally:
        conn.close()


def test_reject_writes_training_pair_only_with_stage5_call_id(client, store_path):
    tenant_id = str(uuid.uuid4())
    call_id = "call:" + uuid.uuid4().hex
    _seed_stage5_row(store_path, call_id, tenant_id)

    llm_assessment = LLMAssessment(
        call_id=call_id,
        match=True,
        llm_confidence=0.6,
        reasoning="looked similar",
        signals_examined=("token_set_ratio",),
        prompt_sha256="deadbeef",
    )
    pending_with_call = _seed_pending(
        store_path,
        tenant_id,
        "CLIENT_LLM",
        score=0.6,
        source_entity_id="llm-1",
        source_id="LLM-1",
        llm_assessment=llm_assessment,
    )

    before = _training_row_count(store_path)
    resp = client.post(
        f"/approvals/{pending_with_call}/reject",
        headers={"X-Nexus-Tenant": tenant_id},
    )
    assert resp.status_code == 200
    after = _training_row_count(store_path)
    assert after == before + 1

    pending_without_call = _seed_pending(
        store_path,
        tenant_id,
        "CLIENT_NOLLM",
        score=0.6,
        source_entity_id="nollm-1",
        source_id="NOLLM-1",
        llm_assessment=None,
    )
    before2 = _training_row_count(store_path)
    resp2 = client.post(
        f"/approvals/{pending_without_call}/reject",
        headers={"X-Nexus-Tenant": tenant_id},
    )
    assert resp2.status_code == 200
    after2 = _training_row_count(store_path)
    assert after2 == before2

    status, _rb, _ra, _outcome = _row_state(store_path, pending_without_call)
    assert status == "rejected"


# ---------------------------------------------------------------------------
# 12. Correct resolves to the human-supplied canonical id throughout
# ---------------------------------------------------------------------------


def test_correct_resolves_to_human_supplied_id(client, store_path):
    tenant_id = str(uuid.uuid4())
    pending_id = _seed_pending(
        store_path, tenant_id, "CLIENT_TOP", canonical_name="top candidate", score=0.8
    )
    _insert_canonical(store_path, "CLIENT_HUMAN", tenant_id, canonical_name="human choice")

    resp = client.post(
        f"/approvals/{pending_id}/correct",
        headers={"X-Nexus-Tenant": tenant_id},
        json={"canonical_id": "CLIENT_HUMAN", "approved_by": "reviewer_2"},
    )
    assert resp.status_code == 200
    assert resp.json()["canonical_id"] == "CLIENT_HUMAN"

    conn = sqlite3.connect(store_path)
    try:
        alias_row = conn.execute(
            "SELECT canonical_id FROM entity_aliases WHERE canonical_id = ?",
            ("CLIENT_HUMAN",),
        ).fetchone()
        top_alias_row = conn.execute(
            "SELECT canonical_id FROM entity_aliases WHERE canonical_id = ?",
            ("CLIENT_TOP",),
        ).fetchone()
        edge_row = conn.execute(
            "SELECT source_node, target_node FROM entity_edges "
            "WHERE source_node = ? AND target_node = ?",
            ("CLIENT_HUMAN", "CLIENT_HUMAN"),
        ).fetchone()
    finally:
        conn.close()

    assert alias_row is not None
    assert top_alias_row is None
    assert edge_row is not None
    assert edge_row == ("CLIENT_HUMAN", "CLIENT_HUMAN")

    status, resolved_by, _resolved_at, outcome = _row_state(store_path, pending_id)
    assert status == "corrected"
    assert resolved_by == "reviewer_2"
    assert outcome == "CLIENT_HUMAN"


# ---------------------------------------------------------------------------
# 13. Second POST against a terminal row
# ---------------------------------------------------------------------------


def test_second_post_against_terminal_row_returns_409_and_writes_nothing(client, store_path):
    tenant_id = str(uuid.uuid4())
    pending_id = _seed_pending(store_path, tenant_id, "CLIENT_TERMINAL", score=0.8)

    first = client.post(
        f"/approvals/{pending_id}/approve", headers={"X-Nexus-Tenant": tenant_id}
    )
    assert first.status_code == 200

    conn = sqlite3.connect(store_path)
    try:
        count_before = conn.execute("SELECT COUNT(*) FROM pending_decisions").fetchone()[0]
    finally:
        conn.close()
    row_before = _row_state(store_path, pending_id)

    second = client.post(
        f"/approvals/{pending_id}/approve", headers={"X-Nexus-Tenant": tenant_id}
    )
    assert second.status_code == 409

    conn = sqlite3.connect(store_path)
    try:
        count_after = conn.execute("SELECT COUNT(*) FROM pending_decisions").fetchone()[0]
    finally:
        conn.close()
    row_after = _row_state(store_path, pending_id)

    assert count_after == count_before
    assert row_after == row_before


# ---------------------------------------------------------------------------
# 14. Ordering under mark-then-write failure
# ---------------------------------------------------------------------------


def test_writer_failure_leaves_row_pending(client, store_path):
    tenant_id = str(uuid.uuid4())
    pending_id = _seed_pending(store_path, tenant_id, "CLIENT_WFAIL", score=0.8)

    def _raise(*args, **kwargs):
        raise RuntimeError("stage6 writer exploded")

    test_client = TestClient(app, raise_server_exceptions=False)
    with mock.patch.object(approvals_mod, "resolve_match", _raise):
        resp = test_client.post(
            f"/approvals/{pending_id}/approve", headers={"X-Nexus-Tenant": tenant_id}
        )
    assert resp.status_code == 500

    status, resolved_by, resolved_at, _outcome = _row_state(store_path, pending_id)
    assert status == "pending"
    assert resolved_by is None
    assert resolved_at is None


def test_mark_decided_failure_after_successful_write_leaves_graph_write_standing(
    client, store_path
):
    tenant_id = str(uuid.uuid4())
    pending_id = _seed_pending(
        store_path, tenant_id, "CLIENT_MFAIL", canonical_name="mark fail co", score=0.8
    )

    def _raise(*args, **kwargs):
        raise RuntimeError("mark_decided exploded")

    test_client = TestClient(app, raise_server_exceptions=False)
    with mock.patch.object(approvals_mod, "mark_decided", _raise):
        resp = test_client.post(
            f"/approvals/{pending_id}/approve", headers={"X-Nexus-Tenant": tenant_id}
        )
    assert resp.status_code == 500

    conn = sqlite3.connect(store_path)
    try:
        alias_row = conn.execute(
            "SELECT canonical_id FROM entity_aliases WHERE canonical_id = ?",
            ("CLIENT_MFAIL",),
        ).fetchone()
    finally:
        conn.close()
    assert alias_row is not None

    status, resolved_by, resolved_at, _outcome = _row_state(store_path, pending_id)
    assert status == "pending"
    assert resolved_by is None
    assert resolved_at is None


# ---------------------------------------------------------------------------
# 15-18. Dashboard shell contract
# ---------------------------------------------------------------------------


def _import_pages_module():
    import dashboard.app  # noqa: F401  (ensure pages are registered)

    return sys.modules["pages.approval_queue"]


def _collect_ids(component):
    ids = []
    comp_id = getattr(component, "id", None)
    if comp_id is not None:
        ids.append(comp_id)
    children = getattr(component, "children", None)
    if isinstance(children, list):
        for child in children:
            ids.extend(_collect_ids(child))
    elif children is not None and hasattr(children, "id"):
        ids.extend(_collect_ids(children))
    return ids


def test_layout_builds_with_no_database_present(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_STORE_PATH", str(tmp_path / "does-not-exist.db"))
    page = _import_pages_module()
    tree = page.layout()
    ids = _collect_ids(tree)
    assert ids


def test_page_exposes_layout_and_matches_head_registration():
    page = _import_pages_module()
    assert hasattr(page, "layout")

    current_src = APPROVAL_QUEUE_PAGE_PATH.read_text()
    result = subprocess.run(
        ["git", "show", "HEAD:dashboard/pages/approval_queue.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    head_src = result.stdout

    pattern = re.compile(
        r"dash\.register_page\(\s*__name__\s*,\s*path\s*=\s*[\"']([^\"']+)[\"']\s*,\s*"
        r"name\s*=\s*[\"']([^\"']+)[\"']"
    )
    current_match = pattern.search(current_src)
    head_match = pattern.search(head_src)
    assert current_match and head_match
    assert current_match.groups() == head_match.groups()


def test_no_regression_to_dashboard_shell_shell_tests():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_dashboard_shell.py",
            "-x",
            "--tb=short",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_badge_callback_populates_reserved_slot(store_path, monkeypatch):
    import dashboard.app as dashboard_app

    tenant_id = str(uuid.uuid4())
    monkeypatch.setenv("NEXUS_TENANT_ID", tenant_id)
    _seed_pending(store_path, tenant_id, "CLIENT_BADGE", score=0.8)

    test_client = dashboard_app.app.server.test_client()
    resp = test_client.get("/approval-queue")
    assert resp.status_code == 200

    badge_key = None
    for key in dashboard_app.app.callback_map:
        if "nav-badge" in key and "/approval-queue" in key:
            badge_key = key
            break
    assert badge_key is not None

    entries = [k for k in dashboard_app.app.callback_map if "nav-badge" in k]
    assert entries

    # The registered entry's `callback` is Dash's context-injecting
    # wrapper (it requires internal dispatch kwargs Dash itself
    # supplies); the plain function it wraps is what this feature
    # exposes as `update_nav_badge`, called directly here.
    page = sys.modules["pages.approval_queue"]
    result = page.update_nav_badge("/approval-queue", None)

    expected = page.pending_count(tenant_id)
    assert result == expected
    assert expected > 0

    head_result = subprocess.run(
        ["git", "show", "HEAD:dashboard/app.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert (REPO_ROOT / "dashboard" / "app.py").read_text() == head_result.stdout


# ---------------------------------------------------------------------------
# 19-21. Table columns / filters / detail view
# ---------------------------------------------------------------------------


def test_table_exposes_spec_columns():
    page = _import_pages_module()
    ids = {c["id"] for c in page.TABLE_COLUMNS}
    assert ids == {
        "incoming_entity_name",
        "candidate_entity_name",
        "source_categories",
        "confidence_score",
        "match_type",
    }


def _fixture_rows():
    return [
        {
            "pending_id": "p1",
            "incoming_entity_name": "Acme Corp",
            "candidate_entity_name": "Acme Consulting",
            "source_categories": "psa:accounting",
            "confidence_score": 0.85,
            "match_type": "scored",
            "entity_category": "organization",
            "category_pair": "psa:accounting",
        },
        {
            "pending_id": "p2",
            "incoming_entity_name": "Jane Smith",
            "candidate_entity_name": "J. Smith",
            "source_categories": "psa:accounting",
            "confidence_score": 0.55,
            "match_type": "llm",
            "entity_category": "person",
            "category_pair": "psa:accounting",
        },
        {
            "pending_id": "p3",
            "incoming_entity_name": "Beta LLC",
            "candidate_entity_name": "Beta Holdings",
            "source_categories": "accounting:crm",
            "confidence_score": 0.72,
            "match_type": "scored",
            "entity_category": "organization",
            "category_pair": "accounting:crm",
        },
    ]


def test_apply_filters_entity_category():
    page = _import_pages_module()
    rows = _fixture_rows()
    result = page.apply_filters(rows, entity_category="organization")
    assert result
    assert len(result) < len(rows)
    assert all(r["entity_category"] == "organization" for r in result)


def test_apply_filters_confidence_range():
    page = _import_pages_module()
    rows = _fixture_rows()
    result = page.apply_filters(rows, min_confidence=0.7, max_confidence=0.9)
    assert result
    assert len(result) < len(rows)
    assert all(0.7 <= r["confidence_score"] <= 0.9 for r in result)


def test_apply_filters_category_pair():
    page = _import_pages_module()
    rows = _fixture_rows()
    result = page.apply_filters(rows, category_pair="psa:accounting")
    assert result
    assert len(result) < len(rows)
    assert all(r["category_pair"] == "psa:accounting" for r in result)


def _fixture_disposition_and_entity(with_llm: bool):
    top = _make_scored_match("CLIENT_DETAIL", 0.8)
    llm_assessment = None
    if with_llm:
        llm_assessment = LLMAssessment(
            call_id="call:detail",
            match=True,
            llm_confidence=0.6,
            reasoning="detail reasoning text",
            signals_examined=("token_set_ratio",),
            prompt_sha256="deadbeef",
        )
    disposition = Disposition(
        source_entity_id="detail-1",
        action="QUEUE_FOR_REVIEW",
        top_match=top,
        candidates_ranked=(top,),
        cluster_conflict=False,
        llm_assessment=llm_assessment,
        tenant_id=None,
    )
    entity = _make_entity("Raw Incoming Name Co", source_id="DETAIL-1")

    from core.matching.pending_store import PendingDecision

    pending = PendingDecision(
        pending_id="pending:detail",
        tenant_id=None,
        decision_key="key",
        status="pending",
        source_entity_id="detail-1",
        action="QUEUE_FOR_REVIEW",
        top_canonical_id="CLIENT_DETAIL",
        top_score=0.8,
        category_pair="psa:accounting",
        cluster_conflict=False,
        abbreviation_rescue=False,
        llm_call_id=llm_assessment.call_id if llm_assessment else None,
        entity_json={},
        disposition_json={},
        proposal_json={},
        created_at="2026-01-01T00:00:00Z",
        resolved_at=None,
        resolved_by=None,
        outcome_canonical_id=None,
    )
    return pending, disposition, entity


def test_detail_view_shows_signal_breakdown_and_graph_evidence():
    page = _import_pages_module()
    pending, disposition, entity = _fixture_disposition_and_entity(with_llm=False)
    tree = page.build_detail_view(pending, disposition, entity, candidate_name="Raw Candidate Co")
    ids = _collect_ids(tree)
    assert "detail-signal-breakdown" in ids
    assert "detail-graph-evidence" in ids
    assert "detail-llm-reasoning" not in ids


def test_detail_view_shows_llm_reasoning_when_present():
    page = _import_pages_module()
    pending, disposition, entity = _fixture_disposition_and_entity(with_llm=True)
    tree = page.build_detail_view(pending, disposition, entity, candidate_name="Raw Candidate Co")
    ids = _collect_ids(tree)
    assert "detail-llm-reasoning" in ids


def test_detail_view_shows_raw_unnormalized_names():
    page = _import_pages_module()
    pending, disposition, entity = _fixture_disposition_and_entity(with_llm=False)
    tree = page.build_detail_view(pending, disposition, entity, candidate_name="Raw Candidate Co")

    def _flatten(component):
        out = [component]
        children = getattr(component, "children", None)
        if isinstance(children, list):
            for child in children:
                out.extend(_flatten(child))
        elif children is not None and hasattr(children, "children"):
            out.extend(_flatten(children))
        return out

    texts = [
        c.children
        for c in _flatten(tree)
        if isinstance(getattr(c, "children", None), str)
    ]
    joined = " ".join(texts)
    assert entity.raw_name in joined
    assert "Raw Candidate Co" in joined


# ---------------------------------------------------------------------------
# 22. Module-level pending-count helper
# ---------------------------------------------------------------------------


def test_pending_count_helper_is_scoped(store_path):
    page = _import_pages_module()
    tenant_a = str(uuid.uuid4())
    tenant_b = str(uuid.uuid4())

    _seed_pending(store_path, tenant_a, "CLIENT_COUNT_A", score=0.8)
    count_a = page.pending_count(tenant_a)
    assert count_a > 0

    conn = sqlite3.connect(store_path)
    try:
        expected = conn.execute(
            "SELECT COUNT(*) FROM pending_decisions WHERE status = 'pending' AND tenant_id = ?",
            (tenant_a,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count_a == expected

    _seed_pending(store_path, tenant_b, "CLIENT_COUNT_B", score=0.8, source_entity_id="cb-1", source_id="CB-1")
    count_a_again = page.pending_count(tenant_a)
    assert count_a_again == count_a


# ---------------------------------------------------------------------------
# 23-24. Connection provider ships and is the only way in
# ---------------------------------------------------------------------------


def test_connection_provider_opens_yields_and_closes(store_path):
    with pending_store_mod.get_connection() as conn:
        conn.execute("SELECT 1").fetchone()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_connection_provider_bootstraps_schema_at_a_fresh_path(tmp_path, monkeypatch):
    """Regression (feature 11 runtime defect): `get_connection` resolved a
    path that nothing ever applied the schema to, so SQLite silently made
    an empty file and every real query died with
    `no such table: pending_decisions`. A fresh path must yield a working
    store, and reopening it must not re-provision over existing rows."""
    from core.matching.pending_store import list_pending

    path = tmp_path / "fresh-store.db"
    assert not path.exists()
    monkeypatch.setenv("NEXUS_STORE_PATH", str(path))

    # A real query against the store's own table, on a path with no schema.
    with pending_store_mod.get_connection() as conn:
        assert list_pending(conn, tenant_id=None) == []

    # And the store is genuinely writable through the normal 10b path,
    # including the graph table `enqueue_pending` tenant-checks against.
    tenant_id = str(uuid.uuid4())
    pending_id = _seed_pending(path, tenant_id, "CLIENT_BOOTSTRAP", score=0.8)

    # Re-opening is idempotent: the second bootstrap pass must not drop,
    # recreate, or otherwise disturb the row written above.
    with pending_store_mod.get_connection() as conn:
        rows = list_pending(conn, tenant_id=tenant_id)
    assert [row.pending_id for row in rows] == [pending_id]


def test_connection_provider_closes_on_exception(store_path):
    captured = {}
    with pytest.raises(RuntimeError):
        with pending_store_mod.get_connection() as conn:
            captured["conn"] = conn
            raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        captured["conn"].execute("SELECT 1")


def test_router_constructs_no_connection_of_its_own(client, store_path):
    result = subprocess.run(
        ["grep", "-nE", r"^\s*import sqlite3|sqlite3\.connect", "api/routers/approvals.py",
         "dashboard/pages/approval_queue.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.stdout == ""

    tenant_id = str(uuid.uuid4())
    pending_id = _seed_pending(store_path, tenant_id, "CLIENT_SPYCONN", score=0.8)

    spy_calls = []
    original = pending_store_mod.get_connection

    def _spy_get_connection():
        spy_calls.append(1)
        return original()

    with mock.patch.object(approvals_mod, "get_connection", _spy_get_connection):
        client.get("/approvals/pending", headers={"X-Nexus-Tenant": tenant_id})
        client.get(f"/approvals/{pending_id}", headers={"X-Nexus-Tenant": tenant_id})

    assert spy_calls


# ---------------------------------------------------------------------------
# 25. 10b's existing contract is unchanged
# ---------------------------------------------------------------------------


# Feature 11 is the revision that added a connection provider to 10b's
# store module. Its presence is what distinguishes a post-11 revision of
# `core/matching/pending_store.py` from the 10b revision this test
# compares against — derived from source text, never from a commit sha.
_FEATURE_11_STORE_MARKER = "def get_connection"

_PENDING_STORE_REL_PATH = "core/matching/pending_store.py"


def _pre_feature11_store_revisions() -> tuple[str, str, str]:
    """Walk `pending_store.py`'s history for the last revision that
    predates feature 11's additions.

    Returns `(introducing_sha, pre_change_sha, pre_change_source)`.
    Walking history — the same approach `_load_pre_registration_main_module`
    takes for `api/main.py` — is what keeps this honest: `HEAD` is the ship
    commit for this feature, so a `HEAD`-based comparison would diff the
    changed file against itself and could never fail.
    """
    shas = subprocess.run(
        ["git", "log", "--format=%H", "--follow", "--", _PENDING_STORE_REL_PATH],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert shas, f"no git history for {_PENDING_STORE_REL_PATH}"
    for index, sha in enumerate(shas):
        content = subprocess.run(
            ["git", "show", f"{sha}:{_PENDING_STORE_REL_PATH}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        if _FEATURE_11_STORE_MARKER in content:
            continue
        assert index > 0, (
            "no revision of pending_store.py introduces "
            f"{_FEATURE_11_STORE_MARKER!r} — this test's marker is stale"
        )
        return shas[index - 1], sha, content
    raise AssertionError(
        f"every revision of {_PENDING_STORE_REL_PATH} already contains "
        f"{_FEATURE_11_STORE_MARKER!r}"
    )


def test_10b_contract_unchanged():
    introducing_sha, pre_change_sha, pre_change_source = _pre_feature11_store_revisions()

    module = types.ModuleType("_pre_feature11_pending_store_test11")
    module.__file__ = str(REPO_ROOT / _PENDING_STORE_REL_PATH)
    sys.modules[module.__name__] = module
    try:
        exec(compile(pre_change_source, module.__file__, "exec"), module.__dict__)
    finally:
        sys.modules.pop(module.__name__, None)

    expected_names = ("list_pending", "get_pending", "rehydrate", "mark_decided")
    fn_names = [name for name in expected_names if hasattr(module, name)]
    # 10b defined all four; a missing one is itself a broken contract, so
    # this is an equality check, not a filter that can silently empty out.
    assert fn_names == list(expected_names), (
        f"{pre_change_sha} is missing 10b store functions: "
        f"{sorted(set(expected_names) - set(fn_names))}"
    )
    for name in fn_names:
        before_sig = inspect.signature(getattr(module, name))
        after_sig = inspect.signature(getattr(pending_store_mod, name))
        assert before_sig == after_sig, f"{name} signature changed since {pre_change_sha}"

    # Feature 11 (including its follow-up fixes) added no migration and
    # altered none. Diffing against the parent of the commit that
    # introduced the feature-11 changes — rather than the working tree
    # against HEAD, which is trivially empty once committed — makes this
    # capable of failing.
    pre_feature_tree = f"{introducing_sha}^"
    listed = subprocess.run(
        ["git", "ls-tree", "--name-only", pre_feature_tree, "db/migrations/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert listed, f"db/migrations/ is empty at {pre_feature_tree}"

    diff = subprocess.run(
        ["git", "diff", pre_feature_tree, "--", "db/migrations/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert diff.stdout == "", (
        f"db/migrations/ changed since {pre_feature_tree}:\n{diff.stdout}"
    )


# ---------------------------------------------------------------------------
# 26. No second-engine / driver references; requirements untouched
# ---------------------------------------------------------------------------


def test_no_pg_engine_or_writeback_table_references():
    # Every fragment below is split so neither the identifier nor the
    # joined pattern string is itself a contiguous match for what it
    # checks for (grep would otherwise flag this test file).
    frag_a = "psy" + "cop" + "g"
    frag_b = "DATA" + "BASE" + "_" + "URL"
    frag_c = "pos" + "tg" + "res"
    frag_d = "appro" + "val_dec" + "isions"
    frag_e = "au" + "dit_l" + "og"
    pattern = "|".join([frag_a, frag_b, frag_c, frag_d, frag_e])
    result = subprocess.run(
        [
            "grep",
            "-rniE",
            pattern,
            "api/routers/approvals.py",
            "dashboard/pages/approval_queue.py",
            "tests/test_approvals_api.py",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.stdout == ""

    result2 = subprocess.run(
        ["git", "diff", "--", "requirements.txt"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result2.stdout == ""
