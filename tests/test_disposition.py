"""Tests for Pipeline Stage 4 (`core.matching.disposition`).

Covers hardened-design §6 disposition bullets
(`features/_adversaries/threshold-llm-fallback.md`): exact threshold
boundaries, empty candidates, cluster conflict detection, deterministic
tie-break, duplicate dedup, tenant scoping on `are_clustered`.
"""

from __future__ import annotations

import pathlib
import sqlite3
from typing import Optional

import pytest

from core.matching.disposition import (
    AUTO_APPROVE_THRESHOLD,
    LLM_FALLBACK_THRESHOLD,
    SURFACE_THRESHOLD,
    apply_thresholds,
)
from core.matching.types import (
    Disposition,
    GraphEvidence,
    ScoredMatch,
    SignalBreakdown,
)


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQLITE_SCHEMA = REPO_ROOT / "db" / "schema_sqlite.sql"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SQLITE_SCHEMA.read_text())
    try:
        yield c
    finally:
        c.close()


def _insert_canonical(
    conn: sqlite3.Connection,
    canonical_id: str,
    canonical_name: str = "x",
    tenant_id: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO canonical_entities (
            canonical_id, tenant_id, canonical_name, entity_type, entity_category, confidence
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (canonical_id, tenant_id, canonical_name, "client", "organization", 0.95),
    )


def _insert_same_as_edge(
    conn: sqlite3.Connection,
    cid_a: str,
    cid_b: str,
) -> None:
    conn.execute(
        """
        INSERT INTO entity_edges (
            source_node, target_node, relationship,
            source_category, target_category, weight
        ) VALUES (?, ?, 'SAME_AS', 'accounting', 'psa', 1.0)
        """,
        (cid_a, cid_b),
    )


def _make_scored_match(canonical_id: str, score: float) -> ScoredMatch:
    return ScoredMatch(
        canonical_id=canonical_id,
        score=score,
        signal_breakdown=SignalBreakdown(
            token_sort_ratio=0.0,
            token_set_ratio=0.0,
            partial_ratio=0.0,
            jaro_winkler=0.0,
            ngram_jaccard=0.0,
            alias_boost_fired=False,
            abbreviation_bonus_fired=False,
        ),
        graph_evidence=GraphEvidence(
            shared_person_count=0,
            shared_person_bonus=0.0,
            neighborhood_overlap_count=0,
            neighborhood_overlap_bonus=0.0,
        ),
        category_pair=("accounting", "psa"),
        weight_profile_id="default_v1",
    )


# ---------------------------------------------------------------------------
# 1. Threshold band boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "score,expected_action",
    [
        (0.95, "AUTO_APPROVE"),
        (0.90, "AUTO_APPROVE"),  # >= 0.90 inclusive
        (0.8999, "QUEUE_FOR_REVIEW"),
        (0.80, "QUEUE_FOR_REVIEW"),
        (0.70, "QUEUE_FOR_REVIEW"),  # >= 0.70 inclusive
        (0.6999, "LLM_FALLBACK"),
        (0.60, "LLM_FALLBACK"),
        (0.50, "LLM_FALLBACK"),  # >= 0.50 inclusive
        (0.4999, "NO_MATCH"),
        (0.00, "NO_MATCH"),
    ],
)
def test_threshold_band_assignment(
    conn: sqlite3.Connection, score: float, expected_action: str
) -> None:
    candidates = (_make_scored_match("CAN-A", score),)
    disp = apply_thresholds("src-1", candidates, conn)
    assert disp.action == expected_action
    assert disp.source_entity_id == "src-1"


def test_threshold_floating_point_just_above_lower_bound(
    conn: sqlite3.Connection,
) -> None:
    """0.7000001 must land in QUEUE_FOR_REVIEW (not LLM_FALLBACK)."""
    disp = apply_thresholds(
        "src-1", (_make_scored_match("CAN-A", 0.7000001),), conn
    )
    assert disp.action == "QUEUE_FOR_REVIEW"


def test_constants_are_exact_floats() -> None:
    assert AUTO_APPROVE_THRESHOLD == 0.90
    assert SURFACE_THRESHOLD == 0.70
    assert LLM_FALLBACK_THRESHOLD == 0.50


# ---------------------------------------------------------------------------
# 2. Empty / NO_MATCH path
# ---------------------------------------------------------------------------


def test_empty_candidates_returns_no_match(conn: sqlite3.Connection) -> None:
    disp = apply_thresholds("src-1", (), conn)
    assert disp.action == "NO_MATCH"
    assert disp.top_match is None
    assert disp.candidates_ranked == ()
    assert disp.cluster_conflict is False
    assert disp.llm_assessment is None


def test_top_below_no_match_threshold_returns_no_match(
    conn: sqlite3.Connection,
) -> None:
    disp = apply_thresholds(
        "src-1", (_make_scored_match("CAN-A", 0.49),), conn
    )
    assert disp.action == "NO_MATCH"
    assert disp.top_match is None


def test_no_match_omits_candidates_ranked_below_floor() -> None:
    """A top_match below 0.50 → NO_MATCH; the ranked tuple may still
    contain the entries (caller can inspect), but top_match is None."""


# ---------------------------------------------------------------------------
# 3. Cluster conflict
# ---------------------------------------------------------------------------


def test_two_high_candidates_distinct_canonicals_triggers_conflict(
    conn: sqlite3.Connection,
) -> None:
    _insert_canonical(conn, "CAN-A")
    _insert_canonical(conn, "CAN-B")
    conn.commit()
    candidates = (
        _make_scored_match("CAN-A", 0.95),
        _make_scored_match("CAN-B", 0.92),
    )
    disp = apply_thresholds("src-1", candidates, conn)
    assert disp.cluster_conflict is True
    assert disp.action == "QUEUE_FOR_REVIEW"  # downgrade from AUTO_APPROVE
    assert disp.top_match is not None and disp.top_match.canonical_id == "CAN-A"


def test_tied_top_scores_triggers_conflict_queue_for_review(
    conn: sqlite3.Connection,
) -> None:
    _insert_canonical(conn, "CAN-A")
    _insert_canonical(conn, "CAN-B")
    conn.commit()
    candidates = (
        _make_scored_match("CAN-B", 0.95),
        _make_scored_match("CAN-A", 0.95),
    )
    disp = apply_thresholds("src-1", candidates, conn)
    assert disp.cluster_conflict is True
    assert disp.action == "QUEUE_FOR_REVIEW"
    # deterministic tiebreak — ascending canonical_id wins
    assert disp.top_match.canonical_id == "CAN-A"


def test_single_high_candidate_auto_approves(conn: sqlite3.Connection) -> None:
    candidates = (_make_scored_match("CAN-A", 0.95),)
    disp = apply_thresholds("src-1", candidates, conn)
    assert disp.action == "AUTO_APPROVE"
    assert disp.cluster_conflict is False


def test_top1_high_top2_below_surface_no_conflict(
    conn: sqlite3.Connection,
) -> None:
    candidates = (
        _make_scored_match("CAN-A", 0.95),
        _make_scored_match("CAN-B", 0.50),  # below SURFACE
    )
    disp = apply_thresholds("src-1", candidates, conn)
    assert disp.action == "AUTO_APPROVE"
    assert disp.cluster_conflict is False


def test_two_mid_candidates_both_above_surface_triggers_conflict(
    conn: sqlite3.Connection,
) -> None:
    """Top-1 at 0.85, top-2 at 0.80 — both in QUEUE_FOR_REVIEW band.
    Conflict flag still trips; action stays QUEUE_FOR_REVIEW."""
    _insert_canonical(conn, "CAN-A")
    _insert_canonical(conn, "CAN-B")
    conn.commit()
    candidates = (
        _make_scored_match("CAN-A", 0.85),
        _make_scored_match("CAN-B", 0.80),
    )
    disp = apply_thresholds("src-1", candidates, conn)
    assert disp.action == "QUEUE_FOR_REVIEW"
    assert disp.cluster_conflict is True


def test_same_as_edge_suppresses_conflict(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-A")
    _insert_canonical(conn, "CAN-B")
    _insert_same_as_edge(conn, "CAN-A", "CAN-B")
    conn.commit()
    candidates = (
        _make_scored_match("CAN-A", 0.95),
        _make_scored_match("CAN-B", 0.92),
    )
    disp = apply_thresholds("src-1", candidates, conn)
    assert disp.cluster_conflict is False
    assert disp.action == "AUTO_APPROVE"  # no override


def test_same_as_edge_reverse_direction_also_suppresses_conflict(
    conn: sqlite3.Connection,
) -> None:
    _insert_canonical(conn, "CAN-A")
    _insert_canonical(conn, "CAN-B")
    _insert_same_as_edge(conn, "CAN-B", "CAN-A")  # B → A, not A → B
    conn.commit()
    candidates = (
        _make_scored_match("CAN-A", 0.95),
        _make_scored_match("CAN-B", 0.92),
    )
    disp = apply_thresholds("src-1", candidates, conn)
    assert disp.cluster_conflict is False
    assert disp.action == "AUTO_APPROVE"


# ---------------------------------------------------------------------------
# 4. Duplicate canonical_id dedup
# ---------------------------------------------------------------------------


def test_duplicate_canonical_id_deduped_with_max_score(
    conn: sqlite3.Connection,
) -> None:
    candidates = (
        _make_scored_match("CAN-A", 0.65),
        _make_scored_match("CAN-A", 0.92),  # higher score wins
        _make_scored_match("CAN-A", 0.55),
    )
    disp = apply_thresholds("src-1", candidates, conn)
    assert len(disp.candidates_ranked) == 1
    assert disp.candidates_ranked[0].canonical_id == "CAN-A"
    assert disp.candidates_ranked[0].score == 0.92
    assert disp.action == "AUTO_APPROVE"
    assert disp.cluster_conflict is False  # only one distinct canonical


# ---------------------------------------------------------------------------
# 5. Deterministic ordering
# ---------------------------------------------------------------------------


def test_candidates_ranked_descending_score_ascending_canonical_id(
    conn: sqlite3.Connection,
) -> None:
    candidates = (
        _make_scored_match("CAN-Z", 0.80),
        _make_scored_match("CAN-A", 0.95),
        _make_scored_match("CAN-M", 0.80),
        _make_scored_match("CAN-B", 0.95),
    )
    disp = apply_thresholds("src-1", candidates, conn)
    assert [m.canonical_id for m in disp.candidates_ranked] == [
        "CAN-A",
        "CAN-B",
        "CAN-M",
        "CAN-Z",
    ]


# ---------------------------------------------------------------------------
# 6. Tenant scoping
# ---------------------------------------------------------------------------


def test_same_as_edge_under_other_tenant_is_ignored(
    conn: sqlite3.Connection,
) -> None:
    _insert_canonical(conn, "CAN-A", tenant_id="t-other")
    _insert_canonical(conn, "CAN-B", tenant_id="t-other")
    _insert_same_as_edge(conn, "CAN-A", "CAN-B")
    conn.commit()
    candidates = (
        _make_scored_match("CAN-A", 0.95),
        _make_scored_match("CAN-B", 0.92),
    )
    # Query under a DIFFERENT tenant → edge is ignored → conflict fires.
    disp = apply_thresholds("src-1", candidates, conn, tenant_id="t-self")
    assert disp.cluster_conflict is True
    assert disp.action == "QUEUE_FOR_REVIEW"


def test_same_as_edge_under_matching_tenant_suppresses_conflict(
    conn: sqlite3.Connection,
) -> None:
    _insert_canonical(conn, "CAN-A", tenant_id="t-self")
    _insert_canonical(conn, "CAN-B", tenant_id="t-self")
    _insert_same_as_edge(conn, "CAN-A", "CAN-B")
    conn.commit()
    candidates = (
        _make_scored_match("CAN-A", 0.95),
        _make_scored_match("CAN-B", 0.92),
    )
    disp = apply_thresholds("src-1", candidates, conn, tenant_id="t-self")
    assert disp.cluster_conflict is False
    assert disp.action == "AUTO_APPROVE"


def test_tenant_id_propagated_to_disposition(conn: sqlite3.Connection) -> None:
    disp = apply_thresholds(
        "src-1", (_make_scored_match("CAN-A", 0.95),), conn, tenant_id="t-abc"
    )
    assert disp.tenant_id == "t-abc"


# ---------------------------------------------------------------------------
# 7. Stage 4 contract: no LLM call
# ---------------------------------------------------------------------------


def test_apply_thresholds_does_not_populate_llm_assessment(
    conn: sqlite3.Connection,
) -> None:
    """Stage 4 never calls the LLM; `llm_assessment` is always None.
    The orchestrator routes LLM_FALLBACK dispositions to llm_assess()."""
    disp = apply_thresholds(
        "src-1", (_make_scored_match("CAN-A", 0.55),), conn
    )
    assert disp.action == "LLM_FALLBACK"
    assert disp.llm_assessment is None


# ---------------------------------------------------------------------------
# 8. Disposition is frozen
# ---------------------------------------------------------------------------


def test_disposition_is_frozen(conn: sqlite3.Connection) -> None:
    disp = apply_thresholds(
        "src-1", (_make_scored_match("CAN-A", 0.95),), conn
    )
    with pytest.raises((AttributeError, Exception)):
        disp.action = "NO_MATCH"  # type: ignore[misc]
