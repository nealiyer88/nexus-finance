"""Cold-start cross-category clustering (feature 13): `cluster_entities`.

Turns the `MatchResult` tuple produced by one or more `run_ingestion` calls
(feature 12) into candidate clusters for guided onboarding review (spec
Section 12, Day 2-3). No new matching, scoring, or LLM logic lives here —
this module only GROUPS results that Stage 1-6 already produced.

Null-tolerant input contract (DECIDED, feature brief): `match()` returns a
`MatchResult` whose `disposition` and `signal_breakdown` are both `None` on
the Stage 1 deterministic short-circuit and on the Stage 2 no-candidates
path — on an empty graph, the entire first connector run takes the
no-candidates path. `cluster_entities` MUST accept those results without
raising. An entity whose `disposition` is absent, or whose
`disposition.top_match` is `None` (the `NO_MATCH` band), belongs to ZERO
clusters — that is expected output, not an error.

LLM-assisted clustering is NOT reimplemented here: Stage 5 (`llm_assess`,
wired into `core.matching.engine.match()`) already ran, redacted via
`core.matching.redaction`, and force-set `action="QUEUE_FOR_REVIEW"` on
every LLM-derived disposition before this module ever sees the result. A
cluster built from an LLM-derived member therefore never recommends
AUTO_APPROVE by construction (see `_recommended_action`), with no
re-derivation of that rule needed here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from core.graph.entity_store import (
    get_aliases_grouped_by_category,
    get_canonical_name_and_category,
)
from core.matching.types import Action, MatchResult


@dataclass(frozen=True)
class ClusterMember:
    """One seeded entity whose disposition pointed at this cluster's
    canonical. `source_category` is the incoming entity's own system
    category, taken from `ScoredMatch.category_pair[0]` (the convention
    Stage 3 writes: `category_pair = (entity.category, candidate_category)`)."""

    source_entity_id: str
    score: float
    source_category: str
    action: Action
    abbreviation_rescue: bool


@dataclass(frozen=True)
class Cluster:
    """One proposed cross-category cluster for guided onboarding review.

    `aliases_by_category` reflects the graph's CURRENT `entity_aliases`
    rows for `canonical_id`, grouped by source category — it is not
    re-derived from member names, since a `MatchResult` carries no entity
    name and a QUEUE_FOR_REVIEW member has not yet been written to the
    graph (Stage 6 only writes an alias on AUTO_APPROVE).
    """

    canonical_id: str
    proposed_canonical_name: str
    aliases_by_category: dict[str, tuple[str, ...]]
    aggregate_confidence: float
    recommended_action: Action
    members: tuple[ClusterMember, ...]


def _recommended_action(members: tuple[ClusterMember, ...]) -> Action:
    """AUTO_APPROVE only when every member's own disposition action was
    AUTO_APPROVE. Stage 5 always converts an LLM-derived disposition to
    QUEUE_FOR_REVIEW before it reaches this module, so a cluster with an
    LLM-derived member never qualifies here — no separate LLM check
    needed."""
    if all(member.action == "AUTO_APPROVE" for member in members):
        return "AUTO_APPROVE"
    return "QUEUE_FOR_REVIEW"


def cluster_entities(
    results: tuple[MatchResult, ...],
    conn: sqlite3.Connection,
    tenant_id: Optional[str] = None,
) -> tuple[Cluster, ...]:
    """Group a seeding run's `MatchResult`s into candidate clusters.

    Clusters derive ONLY from `disposition.top_match.canonical_id` — every
    result whose `disposition` is `None`, or whose `top_match` is `None`
    (Stage 1 hit, Stage 2 no-candidates new-entity path, or Stage 4
    `NO_MATCH`), is skipped and belongs to zero clusters. Ranked by
    aggregate confidence, then by cluster size, both descending;
    `canonical_id` breaks remaining ties for determinism.
    """
    groups: dict[str, list[MatchResult]] = {}
    for result in results:
        disposition = result.disposition
        if disposition is None or disposition.top_match is None:
            continue
        groups.setdefault(disposition.top_match.canonical_id, []).append(result)

    clusters: list[Cluster] = []
    for canonical_id, group_results in groups.items():
        members = tuple(
            ClusterMember(
                source_entity_id=result.source_entity_id,
                score=result.disposition.top_match.score,
                source_category=result.disposition.top_match.category_pair[0],
                action=result.action,
                abbreviation_rescue=result.disposition.abbreviation_rescue,
            )
            for result in group_results
        )

        canonical_row = get_canonical_name_and_category(conn, canonical_id, tenant_id)
        proposed_canonical_name = canonical_row[0] if canonical_row is not None else canonical_id

        aliases_by_category = {
            category: tuple(values)
            for category, values in get_aliases_grouped_by_category(
                conn, canonical_id, tenant_id
            ).items()
        }

        aggregate_confidence = sum(member.score for member in members) / len(members)

        clusters.append(
            Cluster(
                canonical_id=canonical_id,
                proposed_canonical_name=proposed_canonical_name,
                aliases_by_category=aliases_by_category,
                aggregate_confidence=aggregate_confidence,
                recommended_action=_recommended_action(members),
                members=members,
            )
        )

    clusters.sort(key=lambda c: (-c.aggregate_confidence, -len(c.members), c.canonical_id))
    return tuple(clusters)
