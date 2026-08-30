"""Tests for cold-start historical seeding + clustering (feature 13):
`core.ingestion.historical.seed_from_history` and
`core.ingestion.clustering.cluster_entities`.

Pure SQLite, no database server, no network, no live LLM call — follows
the fixture-backed conventions in `tests/test_pipeline.py`.
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
from core.graph.entity_store import create_canonical_entity
from core.graph.resolution import reset_indices_stale_flag
from core.ingestion.clustering import cluster_entities
from core.ingestion.historical import seed_from_history
from core.matching.disposition import AUTO_APPROVE_THRESHOLD
from core.matching.llm_fallback import reset_call_budget
from core.matching.pending_store import list_pending, mark_decided
from core.matching.types import (
    Disposition,
    GraphEvidence,
    MatchResult,
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
    Never touches `ANTHROPIC_API_KEY` or constructs `anthropic.Anthropic`."""

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
    `run_ingestion` pulls — derived at test time, never hardcoded."""
    qb = _qb_connector()
    ruddr = _ruddr_connector()
    total = 0
    for entity_type in _QB_ENTITY_TYPES:
        total += len(qb.read_entities(entity_type, {}))
    for entity_type in _RUDDR_ENTITY_TYPES:
        total += len(ruddr.read_entities(entity_type, {}))
    return total


# ---------------------------------------------------------------------------
# seed_from_history — end to end
# ---------------------------------------------------------------------------


def test_seed_from_history_processes_every_fixture_entity_without_error(
    conn: sqlite3.Connection,
) -> None:
    expected_total = _expected_fixture_total()
    fake_client = FakeLLMClient(
        {"match": False, "confidence": 0.4, "reasoning": "no match", "signals": []}
    )

    result = seed_from_history(
        _qb_connector(), _ruddr_connector(), conn, "tenant-test", llm_client=fake_client
    )

    ingested_total = result.qb_summary.ingested_total + result.ruddr_summary.ingested_total
    assert ingested_total == expected_total
    assert ingested_total > 0


def test_seed_from_history_cluster_count_within_bounds_and_disjoint(
    conn: sqlite3.Connection,
) -> None:
    fake_client = FakeLLMClient(
        {"match": False, "confidence": 0.4, "reasoning": "no match", "signals": []}
    )

    result = seed_from_history(
        _qb_connector(), _ruddr_connector(), conn, "tenant-test", llm_client=fake_client
    )
    seeded_total = result.qb_summary.ingested_total + result.ruddr_summary.ingested_total

    assert len(result.clusters) > 0
    assert len(result.clusters) <= seeded_total

    seen_source_entity_ids: set[str] = set()
    for cluster in result.clusters:
        for member in cluster.members:
            assert member.source_entity_id not in seen_source_entity_ids
            seen_source_entity_ids.add(member.source_entity_id)


def test_seed_from_history_cluster_fields_present(conn: sqlite3.Connection) -> None:
    fake_client = FakeLLMClient(
        {"match": False, "confidence": 0.4, "reasoning": "no match", "signals": []}
    )

    result = seed_from_history(
        _qb_connector(), _ruddr_connector(), conn, "tenant-test", llm_client=fake_client
    )
    assert result.clusters, "fixtures produced no clusters; test proves nothing"

    for cluster in result.clusters:
        assert isinstance(cluster.proposed_canonical_name, str) and cluster.proposed_canonical_name
        assert isinstance(cluster.aliases_by_category, dict)
        for category, values in cluster.aliases_by_category.items():
            assert isinstance(category, str)
            assert isinstance(values, tuple)
        assert 0.0 <= cluster.aggregate_confidence <= 1.0
        assert cluster.recommended_action in ("AUTO_APPROVE", "QUEUE_FOR_REVIEW")
        assert len(cluster.members) > 0


def test_seed_from_history_second_pass_more_auto_approvals(
    conn: sqlite3.Connection,
) -> None:
    fake_client = FakeLLMClient(
        {"match": True, "confidence": 0.9, "reasoning": "looks similar", "signals": ["token_overlap"]}
    )

    first = seed_from_history(
        _qb_connector(), _ruddr_connector(), conn, "tenant-test", llm_client=fake_client
    )
    first_auto_approved = first.qb_summary.auto_approved + first.ruddr_summary.auto_approved

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

    second = seed_from_history(
        _qb_connector(), _ruddr_connector(), conn, "tenant-test", llm_client=fake_client
    )
    second_auto_approved = second.qb_summary.auto_approved + second.ruddr_summary.auto_approved

    assert second_auto_approved > first_auto_approved


def test_seed_from_history_llm_assisted_clustering_uses_injected_client(
    conn: sqlite3.Connection,
) -> None:
    fake_client = FakeLLMClient(
        {"match": True, "confidence": 0.9, "reasoning": "ok", "signals": []}
    )

    result = seed_from_history(
        _qb_connector(), _ruddr_connector(), conn, "tenant-test", llm_client=fake_client
    )

    expected_calls = result.qb_summary.match_type_counts.get(
        "llm", 0
    ) + result.ruddr_summary.match_type_counts.get("llm", 0)
    assert expected_calls > 0, "fixtures produced no Stage 5 entries; test proves nothing"
    assert len(fake_client.calls) == expected_calls

    # No cluster containing an LLM-derived member ever recommends
    # AUTO_APPROVE (llm_assess forces action="QUEUE_FOR_REVIEW" before
    # cluster_entities ever sees the result).
    combined_results = result.qb_summary.results + result.ruddr_summary.results
    llm_source_entity_ids = {
        r.source_entity_id for r in combined_results if r.match_type == "llm"
    }
    assert llm_source_entity_ids, "no llm match_type results; test proves nothing"
    for cluster in result.clusters:
        member_ids = {member.source_entity_id for member in cluster.members}
        if member_ids & llm_source_entity_ids:
            assert cluster.recommended_action == "QUEUE_FOR_REVIEW"


def test_no_second_redaction_implementation() -> None:
    """The new modules must not reimplement redaction — they rely entirely
    on the shipped `llm_assess` path inside `run_ingestion` / `match()`."""
    historical_src = (REPO_ROOT / "core" / "ingestion" / "historical.py").read_text()
    clustering_src = (REPO_ROOT / "core" / "ingestion" / "clustering.py").read_text()
    for src in (historical_src, clustering_src):
        assert "from core.matching.redaction" not in src
        assert "import core.matching.redaction" not in src
        assert "def redact_org" not in src
        assert "def redact_person" not in src


# ---------------------------------------------------------------------------
# cluster_entities — unit-level, null-tolerant + threshold-grouping contract
# ---------------------------------------------------------------------------


def _signal_breakdown() -> SignalBreakdown:
    return SignalBreakdown(
        token_sort_ratio=95.0,
        token_set_ratio=95.0,
        partial_ratio=95.0,
        jaro_winkler=0.95,
        ngram_jaccard=0.9,
        alias_boost_fired=False,
        abbreviation_bonus_fired=False,
    )


def _scored_match(canonical_id: str, score: float, category_pair: tuple[str, str]) -> ScoredMatch:
    return ScoredMatch(
        canonical_id=canonical_id,
        score=score,
        signal_breakdown=_signal_breakdown(),
        graph_evidence=GraphEvidence(
            shared_person_count=0,
            shared_person_bonus=0.0,
            neighborhood_overlap_count=0,
            neighborhood_overlap_bonus=0.0,
        ),
        category_pair=category_pair,
        weight_profile_id="test-profile",
    )


def _disposition(
    source_entity_id: str, top_match: ScoredMatch, action: str
) -> Disposition:
    return Disposition(
        source_entity_id=source_entity_id,
        action=action,
        top_match=top_match,
        candidates_ranked=(top_match,),
        cluster_conflict=False,
        llm_assessment=None,
        tenant_id="tenant-test",
    )


def test_cluster_entities_null_tolerant_of_missing_disposition_and_signal_breakdown(
    conn: sqlite3.Connection,
) -> None:
    """A `MatchResult` with `disposition=None` and `signal_breakdown=None`
    (Stage 1 hit / Stage 2 no-candidates path) must not raise, and
    contributes zero clusters."""
    no_disposition_result = MatchResult(
        source_entity_id="src-1",
        canonical_id="CLIENT_00000001",
        confidence=1.0,
        match_type="deterministic",
        action="AUTO_APPROVE",
        signal_breakdown=None,
        disposition=None,
        reasoning_trace="stage 1 hit",
    )

    clusters = cluster_entities((no_disposition_result,), conn, tenant_id=None)
    assert clusters == ()


def test_cluster_entities_auto_approve_pairs_grouped_together(
    conn: sqlite3.Connection,
) -> None:
    canonical_id = create_canonical_entity(
        conn,
        canonical_name="Cenlar FSB",
        entity_type="client",
        entity_category="organization",
        confidence=0.97,
        tenant_id=None,
    )
    conn.commit()

    top_match = _scored_match(canonical_id, AUTO_APPROVE_THRESHOLD, ("psa", "accounting"))
    disposition = _disposition("ruddr-src-1", top_match, "AUTO_APPROVE")
    result = MatchResult(
        source_entity_id="ruddr-src-1",
        canonical_id=canonical_id,
        confidence=top_match.score,
        match_type="scored",
        action="AUTO_APPROVE",
        signal_breakdown=top_match.signal_breakdown,
        disposition=disposition,
        reasoning_trace="scored match",
    )

    clusters = cluster_entities((result,), conn, tenant_id=None)

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.canonical_id == canonical_id
    assert cluster.proposed_canonical_name == "Cenlar FSB"
    assert cluster.recommended_action == "AUTO_APPROVE"
    assert len(cluster.members) == 1
    assert cluster.members[0].source_entity_id == "ruddr-src-1"
    assert cluster.members[0].score >= AUTO_APPROVE_THRESHOLD


def test_cluster_entities_no_match_disposition_excluded(conn: sqlite3.Connection) -> None:
    """A disposition whose action is NO_MATCH always carries
    `top_match=None` (Disposition's own contract) — excluded, not an
    error."""
    disposition = Disposition(
        source_entity_id="src-2",
        action="NO_MATCH",
        top_match=None,
        candidates_ranked=(),
        cluster_conflict=False,
        llm_assessment=None,
        tenant_id=None,
    )
    result = MatchResult(
        source_entity_id="src-2",
        canonical_id="CLIENT_00000002",
        confidence=0.0,
        match_type="none",
        action="NO_MATCH",
        signal_breakdown=None,
        disposition=disposition,
        reasoning_trace="no match",
    )

    clusters = cluster_entities((result,), conn, tenant_id=None)
    assert clusters == ()


def test_full_suite_collection_sanity() -> None:
    """Guards against a deselected-everything false-green (acceptance
    criterion 9): pytest's own collection output for this module must
    report a collected count greater than zero. A bare exit code is not
    sufficient evidence of a real pass.

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
            str(REPO_ROOT / "tests" / "test_historical.py"),
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
