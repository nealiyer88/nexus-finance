"""Cold-start historical seeding (feature 13): `seed_from_history`.

Spec Section 12, Days 1-3: on Day 1 the graph is empty, so the first sync
cycle produces zero auto-approvals unless historical data is ingested and
clustered first. This module drives the shipped feature 11/12 pipeline
over both connectors' full entity sets and hands the combined result to
`core.ingestion.clustering.cluster_entities` for guided onboarding.

No re-normalization, re-matching, or re-redaction happens here: connectors
already return `NormalizedEntity` (no Stage 0 call in this feature), and
`run_ingestion` already drives Stage 1-6 via `core.matching.engine.match()`
per entity — including the Stage 5 LLM fallback path and its degrade-on-
budget-exhaustion behavior. Cold start runs on the shipped steady-state
thresholds; no relaxed or widened band exists for it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from connectors.base import ConnectorInterface
from core.ingestion.clustering import Cluster, cluster_entities
from core.ingestion.pipeline import IngestionSummary, run_ingestion

if TYPE_CHECKING:
    from core.matching.llm_fallback import LLMClient


@dataclass(frozen=True)
class HistoricalSeedResult:
    """Outcome of one `seed_from_history` call: the two connectors' own
    `run_ingestion` summaries plus the cross-category clusters derived
    from their combined results."""

    qb_summary: IngestionSummary
    ruddr_summary: IngestionSummary
    clusters: tuple[Cluster, ...]


def seed_from_history(
    qb_connector: ConnectorInterface,
    ruddr_connector: ConnectorInterface,
    conn: sqlite3.Connection,
    tenant_id: str,
    llm_client: Optional["LLMClient"] = None,
) -> HistoricalSeedResult:
    """Cold-start historical seeding + clustering over both V1 connectors.

    Calls `run_ingestion` once per connector against the same `conn` — QB
    first, then RUDDR — so RUDDR's Stage 2 blocking pass sees the
    QB-seeded graph (the index-rebuild policy inside `match()` makes this
    visible mid-run; see `core.ingestion.pipeline` / `core.matching.engine`).
    Each `run_ingestion` call pulls entity types via the pipeline's own
    per-category mapping; this function does not re-list entity types or
    request more than one canonical entity type per connector category.

    The combined `results` tuple from both summaries is handed to
    `cluster_entities` for guided-onboarding grouping. Historical data
    populates the graph layer only — no source-system write ever happens
    on this path (`run_ingestion` / `match()` never call a connector's
    write methods).
    """
    qb_summary = run_ingestion(qb_connector, conn, tenant_id, llm_client=llm_client)
    ruddr_summary = run_ingestion(ruddr_connector, conn, tenant_id, llm_client=llm_client)

    combined_results = qb_summary.results + ruddr_summary.results
    clusters = cluster_entities(combined_results, conn, tenant_id)

    return HistoricalSeedResult(
        qb_summary=qb_summary,
        ruddr_summary=ruddr_summary,
        clusters=clusters,
    )
