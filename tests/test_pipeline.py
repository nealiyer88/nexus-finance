"""Tests for the batch ingestion entry point (`core.ingestion.pipeline.run_ingestion`).

End-to-end: both shipped connectors' fixture entities, through the full
Stage 0-6 pipeline, against an empty then a re-run graph. Pure SQLite, no
database server, no network. Schema is loaded from files, following the
`REPO_ROOT` / path-constant pattern in `tests/test_pending_decisions.py`.
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
import subprocess
import sys
from typing import Any

import pytest

from connectors.quickbooks import QuickBooksConnector
from connectors.ruddr import RUDDRConnector
from core.graph.resolution import reset_indices_stale_flag
from core.ingestion.pipeline import run_ingestion
from core.matching.llm_fallback import reset_call_budget
from core.matching.pending_store import list_pending, mark_decided


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQLITE_SCHEMA = REPO_ROOT / "db" / "schema_sqlite.sql"
TRAINING_MIGRATION = REPO_ROOT / "db" / "migrations" / "002_llm_training_data_sqlite.sql"
PENDING_MIGRATION_CANDIDATES = sorted(
    REPO_ROOT.glob("db/migrations/*_pending_decisions_sqlite.sql")
)
assert PENDING_MIGRATION_CANDIDATES, "no *_pending_decisions_sqlite.sql migration found"
PENDING_MIGRATION = PENDING_MIGRATION_CANDIDATES[0]

QB_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "qb_entities.json"
RUDDR_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "ruddr_entities.json"

_QB_ENTITY_TYPES = ("customer", "vendor", "person")
_RUDDR_ENTITY_TYPES = ("client", "vendor", "person")


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


def _qb_connector() -> QuickBooksConnector:
    return QuickBooksConnector(
        tenant_id="tenant-test",
        client_id="cid",
        client_secret="csecret",
        realm_id="9999",
        fixture_path=str(QB_FIXTURE),
    )


def _ruddr_connector() -> RUDDRConnector:
    return RUDDRConnector(
        tenant_id="tenant-test",
        api_key="ruddr-key",
        fixture_path=str(RUDDR_FIXTURE),
    )


class FakeLLMClient:
    """Minimal `LLMClient` Protocol implementation; records every call.
    No test using this fake ever constructs `anthropic.Anthropic()` or
    reads `ANTHROPIC_API_KEY` — the fake is passed explicitly on every
    `run_ingestion` call in this module."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def assess(
        self, system_prompt: str, user_prompt: str, tool_spec: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((system_prompt, user_prompt, tool_spec))
        return dict(self._response)


def _expected_fixture_total() -> int:
    """Count what the connectors actually return for the entity types
    `run_ingestion` pulls, exactly matching its own internal type lists."""
    qb = _qb_connector()
    ruddr = _ruddr_connector()
    total = 0
    for entity_type in _QB_ENTITY_TYPES:
        total += len(qb.read_entities(entity_type, {}))
    for entity_type in _RUDDR_ENTITY_TYPES:
        total += len(ruddr.read_entities(entity_type, {}))
    return total


# ---------------------------------------------------------------------------
# End-to-end + invariants
# ---------------------------------------------------------------------------


def test_run_ingestion_processes_every_fixture_entity_without_error(
    conn: sqlite3.Connection,
) -> None:
    expected_total = _expected_fixture_total()
    fake_client = FakeLLMClient(
        {"match": False, "confidence": 0.4, "reasoning": "no match", "signals": []}
    )

    qb_summary = run_ingestion(_qb_connector(), conn, "tenant-test", llm_client=fake_client)
    ruddr_summary = run_ingestion(_ruddr_connector(), conn, "tenant-test", llm_client=fake_client)

    ingested_total = qb_summary.ingested_total + ruddr_summary.ingested_total
    assert ingested_total == expected_total
    assert ingested_total > 0


def test_run_ingestion_bucket_accounting_invariant(conn: sqlite3.Connection) -> None:
    fake_client = FakeLLMClient(
        {"match": False, "confidence": 0.4, "reasoning": "no match", "signals": []}
    )
    summary = run_ingestion(_qb_connector(), conn, "tenant-test", llm_client=fake_client)

    assert summary.auto_approved + summary.queued_for_review + summary.no_match == summary.ingested_total
    for result in summary.results:
        bucket_hits = sum(
            [
                result.action == "AUTO_APPROVE",
                result.action == "QUEUE_FOR_REVIEW",
                result.action == "NO_MATCH",
            ]
        )
        assert bucket_hits == 1


def test_run_ingestion_first_run_every_entity_accounted_for(
    conn: sqlite3.Connection,
) -> None:
    fake_client = FakeLLMClient(
        {"match": False, "confidence": 0.4, "reasoning": "no match", "signals": []}
    )
    qb_summary = run_ingestion(_qb_connector(), conn, "tenant-test", llm_client=fake_client)
    ruddr_summary = run_ingestion(_ruddr_connector(), conn, "tenant-test", llm_client=fake_client)

    combined_results = qb_summary.results + ruddr_summary.results
    assert len(combined_results) == qb_summary.ingested_total + ruddr_summary.ingested_total
    for result in combined_results:
        assert result.action in ("AUTO_APPROVE", "QUEUE_FOR_REVIEW", "NO_MATCH")


def test_run_ingestion_second_run_auto_approve_share_increases(
    conn: sqlite3.Connection,
) -> None:
    fake_client = FakeLLMClient(
        {"match": True, "confidence": 0.9, "reasoning": "looks similar", "signals": ["token_overlap"]}
    )

    run_ingestion(_qb_connector(), conn, "tenant-test", llm_client=fake_client)
    first_ruddr_summary = run_ingestion(_ruddr_connector(), conn, "tenant-test", llm_client=fake_client)

    # Simulate approvals: mark every still-pending row decided.
    for pending in list_pending(conn, tenant_id=None, limit=1000):
        mark_decided(
            conn,
            pending.pending_id,
            status="approved",
            resolved_by="test-reviewer",
            outcome_canonical_id=pending.top_canonical_id,
            tenant_id=None,
        )
    conn.commit()

    second_ruddr_summary = run_ingestion(_ruddr_connector(), conn, "tenant-test", llm_client=fake_client)

    def _auto_approve_share(summary) -> float:
        if summary.ingested_total == 0:
            return 0.0
        return summary.auto_approved / summary.ingested_total

    first_share = _auto_approve_share(first_ruddr_summary)
    second_share = _auto_approve_share(second_ruddr_summary)
    assert second_share > first_share


def test_run_ingestion_pending_persistence_invariant(conn: sqlite3.Connection) -> None:
    fake_client = FakeLLMClient(
        {"match": False, "confidence": 0.55, "reasoning": "uncertain", "signals": []}
    )
    run_ingestion(_qb_connector(), conn, "tenant-test", llm_client=fake_client)

    # Isolate the ruddr run's own before/after delta on the pending table,
    # rather than a pooled cross-run total, so this actually checks the
    # brief's literal equality invariant: rows persisted by THIS run must
    # equal THIS run's own queued_for_review bucket count.
    before_count = conn.execute("SELECT COUNT(*) FROM pending_decisions").fetchone()[0]
    summary = run_ingestion(_ruddr_connector(), conn, "tenant-test", llm_client=fake_client)
    after_count = conn.execute("SELECT COUNT(*) FROM pending_decisions").fetchone()[0]

    assert after_count - before_count == summary.queued_for_review


def test_run_ingestion_reingest_refreshes_pending_rows_without_duplicating(
    conn: sqlite3.Connection,
) -> None:
    """`enqueue_pending` is idempotent on its decision key, so a SECOND
    ingestion of the SAME connector refreshes live rows in place rather
    than inserting new ones. The equality invariant asserted by
    `test_run_ingestion_pending_persistence_invariant` therefore holds
    only on a connector's FIRST run; on a re-run the row delta is
    strictly smaller than the queued count. This test pins that as
    deliberate behaviour rather than leaving it untested — an
    accidental loss of idempotency would show up here as duplicate
    decision keys."""
    fake_client = FakeLLMClient(
        {"match": False, "confidence": 0.55, "reasoning": "uncertain", "signals": []}
    )
    first_summary = run_ingestion(_qb_connector(), conn, "tenant-test", llm_client=fake_client)
    assert first_summary.queued_for_review > 0  # otherwise this proves nothing

    before_count = conn.execute("SELECT COUNT(*) FROM pending_decisions").fetchone()[0]
    second_summary = run_ingestion(_qb_connector(), conn, "tenant-test", llm_client=fake_client)
    after_count = conn.execute("SELECT COUNT(*) FROM pending_decisions").fetchone()[0]

    delta = after_count - before_count
    assert delta < second_summary.queued_for_review
    # Idempotency is on `decision_key`: no key may ever be duplicated.
    distinct_keys = conn.execute(
        "SELECT COUNT(DISTINCT decision_key) FROM pending_decisions"
    ).fetchone()[0]
    assert distinct_keys == after_count


def test_run_ingestion_match_type_distribution_tracked(conn: sqlite3.Connection) -> None:
    fake_client = FakeLLMClient(
        {"match": False, "confidence": 0.4, "reasoning": "no match", "signals": []}
    )
    summary = run_ingestion(_qb_connector(), conn, "tenant-test", llm_client=fake_client)

    assert sum(summary.match_type_counts.values()) == summary.ingested_total
    for match_type in summary.match_type_counts:
        assert match_type in ("deterministic", "scored", "llm", "new", "none")


def test_run_ingestion_reports_bucket_and_ingested_totals(conn: sqlite3.Connection) -> None:
    fake_client = FakeLLMClient(
        {"match": False, "confidence": 0.4, "reasoning": "no match", "signals": []}
    )
    summary = run_ingestion(_qb_connector(), conn, "tenant-test", llm_client=fake_client)

    assert summary.ingested_total == len(summary.results)
    assert isinstance(summary.auto_approved, int)
    assert isinstance(summary.queued_for_review, int)
    assert isinstance(summary.no_match, int)


def test_run_ingestion_resets_call_budget_and_uses_injected_client_only(
    conn: sqlite3.Connection,
) -> None:
    # A response that forces LLM_FALLBACK-band scores won't reliably occur
    # via natural fixture pairing, but the budget-reset contract itself is
    # directly testable: run twice, and confirm no LLMBudgetExceededError
    # ever escapes as an unhandled exception (it would either not fire at
    # all, since no real LLM_FALLBACK band scores occur here, or be caught
    # and converted to a queued MatchResult).
    fake_client = FakeLLMClient(
        {"match": True, "confidence": 0.9, "reasoning": "ok", "signals": []}
    )
    qb_summary = run_ingestion(_qb_connector(), conn, "tenant-test", llm_client=fake_client)
    ruddr_summary = run_ingestion(_ruddr_connector(), conn, "tenant-test", llm_client=fake_client)

    # No live API call: FakeLLMClient never touches ANTHROPIC_API_KEY or
    # constructs anthropic.Anthropic. Its own call count is the only
    # assertable evidence Stage 5 used the injected fake.
    #
    # The expected count is DERIVED from the runs' own summaries, never
    # hardcoded: `core.matching.engine.match` stamps match_type="llm" on
    # exactly the entities that entered Stage 5, and `llm_assess` issues
    # exactly one `client.assess` call per entry. So the fake's recorded
    # call count must equal the two runs' combined "llm" match_type
    # count. A hardcoded number here would self-invalidate the moment an
    # earlier feature changed the fixtures.
    expected_calls = qb_summary.match_type_counts.get(
        "llm", 0
    ) + ruddr_summary.match_type_counts.get("llm", 0)
    assert expected_calls > 0, "fixtures produced no Stage 5 entries; test proves nothing"
    assert len(fake_client.calls) == expected_calls
    # Budget reset contract: the per-run budget is reset at the top of
    # every run_ingestion, so neither run can be bounded by the other's
    # usage — each run's own llm count is served in full.
    assert len(fake_client.calls) > ruddr_summary.match_type_counts.get("llm", 0)


def test_run_ingestion_second_connector_sees_first_connectors_writes(
    conn: sqlite3.Connection,
) -> None:
    """The index-rebuild policy (exercised directly in
    `tests/test_engine.py`) is what makes this possible: without a
    mid-run rebuild, every entity written by the QB run would be
    invisible to RUDDR's blocking pass, and NO cross-connector match
    could ever be found in a single `run_ingestion` pair."""
    fake_client = FakeLLMClient(
        {"match": False, "confidence": 0.4, "reasoning": "no match", "signals": []}
    )
    qb_summary = run_ingestion(_qb_connector(), conn, "tenant-test", llm_client=fake_client)
    assert qb_summary.no_match > 0  # at least one canonical now exists to find

    ruddr_summary = run_ingestion(_ruddr_connector(), conn, "tenant-test", llm_client=fake_client)
    # Some RUDDR entity must find a candidate against the QB-seeded graph
    # (deterministic hit or scored match) — i.e. not every RUDDR entity
    # falls through to a brand-new canonical with zero corroboration.
    assert (ruddr_summary.auto_approved + ruddr_summary.queued_for_review) > 0


def test_run_ingestion_unsupported_connector_category_raises(
    conn: sqlite3.Connection,
) -> None:
    class _FakeConnector:
        category = "crm"

        def read_entities(self, entity_type: str, filters: dict[str, Any]):
            return []

    with pytest.raises(ValueError):
        run_ingestion(_FakeConnector(), conn, "tenant-test")


def test_full_suite_collection_sanity() -> None:
    """Guards against a deselected-everything false-green: pytest's own
    collection output, for feature 12's two new test modules, must
    report a collected count greater than zero. A bare exit code is not
    sufficient evidence of a real pass (acceptance criterion 11).

    `--collect-only` imports the modules but executes no test bodies, so
    the subprocess cannot re-enter this test; it is additionally scoped
    to the two named modules rather than the whole suite.

    Invocation is the repo's documented form (`.venv/bin/python -m
    pytest`, CLAUDE.md): bare `pytest` is not on PATH, and
    `.venv/bin/pytest` fails to put the repo root on `sys.path`.
    """
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    interpreter = str(venv_python) if venv_python.exists() else sys.executable

    completed = subprocess.run(
        [
            interpreter,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            str(REPO_ROOT / "tests" / "test_engine.py"),
            str(REPO_ROOT / "tests" / "test_pipeline.py"),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 0, (
        f"collection failed (rc={completed.returncode}):\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
    match = re.search(r"(\d+)\s+tests?\s+collected", completed.stdout)
    assert match is not None, (
        "could not parse a collected count from pytest output:\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
    collected = int(match.group(1))
    assert collected > 0
