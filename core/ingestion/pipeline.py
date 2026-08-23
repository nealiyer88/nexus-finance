"""Batch ingestion entry point (feature 12): `run_ingestion()`.

Pulls every supported entity type from ONE connector (multi-connector
orchestration is a later scheduling layer's job — NOT-SCOPE), matches
each entity against the graph via `core.matching.engine.match()`, and
returns a summary whose buckets are mutually exclusive and exhaustive
over the entities ingested.

Sequential only — no async, no parallel, no queue.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from connectors.base import ConnectorInterface
from core.matching.engine import match
from core.matching.indices import EmbeddingIndex, NgramIndex, TokenIndex
from core.matching.llm_fallback import reset_call_budget
from core.matching.types import MatchContext, MatchResult

if TYPE_CHECKING:
    from core.matching.llm_fallback import LLMClient


# One canonical entity type per source (see the alias trap documented in
# the build prompt): pulling both "customer" and "client" for the SAME
# connector would double-fetch the aliased QB rows and collide
# (source, source_id) in the pending-decision decision key. V1 connector
# set only (QB category="accounting", RUDDR category="psa").
_ENTITY_TYPES_BY_CATEGORY: dict[str, tuple[str, ...]] = {
    "accounting": ("customer", "vendor", "person"),
    "psa": ("client", "vendor", "person"),
}


def _entity_types_for(connector: ConnectorInterface) -> tuple[str, ...]:
    types = _ENTITY_TYPES_BY_CATEGORY.get(connector.category)
    if types is None:
        raise ValueError(
            f"run_ingestion has no supported entity-type list for connector "
            f"category {connector.category!r}"
        )
    return types


@dataclass(frozen=True)
class IngestionSummary:
    """Bucket accounting is over `MatchResult.action` — the three values
    an orchestrator run can ever end on (LLM_FALLBACK never survives to
    a final `MatchResult`, see `core.matching.engine`). Pairwise
    disjoint and exhaustive: `auto_approved + queued_for_review +
    no_match == ingested_total` always. `match_type_counts` is a
    separate, non-exclusive-bucket view over the five paths
    `match()` can take."""

    ingested_total: int
    auto_approved: int
    queued_for_review: int
    no_match: int
    match_type_counts: dict[str, int]
    results: tuple[MatchResult, ...]


def _summarize(results: list[MatchResult]) -> IngestionSummary:
    action_counts = Counter(r.action for r in results)
    match_type_counts = Counter(r.match_type for r in results)
    return IngestionSummary(
        ingested_total=len(results),
        auto_approved=action_counts.get("AUTO_APPROVE", 0),
        queued_for_review=action_counts.get("QUEUE_FOR_REVIEW", 0),
        no_match=action_counts.get("NO_MATCH", 0),
        match_type_counts=dict(match_type_counts),
        results=tuple(results),
    )


def run_ingestion(
    connector: ConnectorInterface,
    conn: sqlite3.Connection,
    tenant_id: str,
    llm_client: Optional["LLMClient"] = None,
) -> IngestionSummary:
    """Pull every supported entity type from `connector`, match each
    entity against the graph, and return the run summary.

    Connectors already return `NormalizedEntity` from `read_entities` —
    no re-normalization happens here. Resets the Stage 5 call budget at
    the start of the run so a prior run's usage never bounds this one.
    Processing is strictly sequential.
    """
    reset_call_budget()

    entity_types = _entity_types_for(connector)

    ctx = MatchContext(
        conn=conn,
        token_index=TokenIndex.build(conn, tenant_id),
        ngram_index=NgramIndex.build(conn, tenant_id),
        tenant_id=tenant_id,
        embedding_index=EmbeddingIndex.build(conn, tenant_id),
        llm_client=llm_client,
    )

    results: list[MatchResult] = []
    for entity_type in entity_types:
        entities = connector.read_entities(entity_type, {})
        for entity in entities:
            results.append(match(entity, ctx))

    return _summarize(results)
