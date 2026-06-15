"""Pipeline Stage 4: Threshold + Cluster Conflict Detection.

Turns a tuple of `ScoredMatch` (Stage 3 output) into a `Disposition`
without writing to SQLite. Reads only `entity_edges` (via
`are_clustered`) to check whether the top-2 distinct candidates are
already linked by a SAME_AS edge.

Threshold bands (inclusive lower bound, exclusive upper bound where it
matters; the `>=` rule is applied uniformly across all four zones):

    score >= AUTO_APPROVE_THRESHOLD (0.90)         → AUTO_APPROVE
    SURFACE_THRESHOLD <= score < AUTO_APPROVE      → QUEUE_FOR_REVIEW
    LLM_FALLBACK_THRESHOLD <= score < SURFACE      → LLM_FALLBACK
    score < LLM_FALLBACK_THRESHOLD (0.50)          → NO_MATCH

Cluster conflict: when the top-2 distinct `canonical_id`s both score
`>= SURFACE_THRESHOLD` AND `are_clustered(top_1, top_2)` is False, the
top match cannot AUTO_APPROVE — disposition is downgraded to
`QUEUE_FOR_REVIEW` with `cluster_conflict=True`. This applies
regardless of whether the top score is in the AUTO_APPROVE band. The
override never UPGRADES a lower-band action — QUEUE_FOR_REVIEW or
LLM_FALLBACK bands stay where they are.

Tie-break: when two ScoredMatches have identical scores, they are
sorted ascending by `canonical_id` (matching Stage 3's existing
ordering convention). This makes test outcomes deterministic.

Stage 4 does NOT call the LLM. When `action == LLM_FALLBACK`, the
orchestrator is expected to call `core.matching.llm_fallback.llm_assess`
to populate `llm_assessment` (which always downgrades to
QUEUE_FOR_REVIEW).
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from core.graph.entity_store import are_clustered
from core.matching.types import Action, Disposition, ScoredMatch


AUTO_APPROVE_THRESHOLD: float = 0.90
SURFACE_THRESHOLD: float = 0.70
LLM_FALLBACK_THRESHOLD: float = 0.50


def _dedup_by_canonical(
    candidates: tuple[ScoredMatch, ...],
) -> tuple[ScoredMatch, ...]:
    """Keep one entry per canonical_id (the one with the max score), then
    sort descending by score with ascending canonical_id as the
    deterministic tiebreaker.
    """
    best: dict[str, ScoredMatch] = {}
    for cand in candidates:
        existing = best.get(cand.canonical_id)
        if existing is None or cand.score > existing.score:
            best[cand.canonical_id] = cand
    # Sort: high score first; ascending canonical_id on ties.
    return tuple(
        sorted(
            best.values(),
            key=lambda m: (-m.score, m.canonical_id),
        )
    )


def _band(score: float) -> Action:
    """Map a [0.0, 1.0]-clamped score to its threshold band."""
    if score >= AUTO_APPROVE_THRESHOLD:
        return "AUTO_APPROVE"
    if score >= SURFACE_THRESHOLD:
        return "QUEUE_FOR_REVIEW"
    if score >= LLM_FALLBACK_THRESHOLD:
        return "LLM_FALLBACK"
    return "NO_MATCH"


def apply_thresholds(
    source_entity_id: str,
    candidates: tuple[ScoredMatch, ...],
    conn: sqlite3.Connection,
    tenant_id: Optional[str] = None,
) -> Disposition:
    """Apply thresholds + cluster-conflict detection to a candidate set.

    See module docstring for the band rules and conflict override.
    Inputs may be in any order; the function dedupes by canonical_id
    (max score wins) and then sorts the result deterministically.
    """
    ranked = _dedup_by_canonical(candidates)

    if not ranked or ranked[0].score < LLM_FALLBACK_THRESHOLD:
        return Disposition(
            source_entity_id=source_entity_id,
            action="NO_MATCH",
            top_match=None,
            candidates_ranked=ranked,
            cluster_conflict=False,
            llm_assessment=None,
            tenant_id=tenant_id,
        )

    top = ranked[0]
    base_action = _band(top.score)

    cluster_conflict = False
    if len(ranked) >= 2:
        second = ranked[1]
        if (
            top.canonical_id != second.canonical_id
            and top.score >= SURFACE_THRESHOLD
            and second.score >= SURFACE_THRESHOLD
            and not are_clustered(conn, top.canonical_id, second.canonical_id, tenant_id)
        ):
            cluster_conflict = True

    final_action: Action = base_action
    if cluster_conflict and base_action == "AUTO_APPROVE":
        final_action = "QUEUE_FOR_REVIEW"

    return Disposition(
        source_entity_id=source_entity_id,
        action=final_action,
        top_match=top,
        candidates_ranked=ranked,
        cluster_conflict=cluster_conflict,
        llm_assessment=None,
        tenant_id=tenant_id,
    )
