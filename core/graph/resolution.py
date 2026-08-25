"""Pipeline Stage 6: Resolution + Graph Update (SQLite).

Turns a Stage 4/5 `Disposition` plus a resolved human/auto decision into
graph writes:

    resolve_match      — add alias, upsert-or-increment a SAME_AS-style
                          edge, mark indices stale, store a positive
                          training pair (LLM-gated).
    create_new_entity   — create the canonical node, insert
                          system_references rows, initialize edges to
                          related canonicals, mark indices stale, store
                          negative training pairs for the rejected
                          candidates (LLM-gated).
    reject_match        — no graph mutation; store a hard-negative
                          training pair with the full signal breakdown
                          (LLM-gated).

Each public function owns exactly ONE SQLite transaction: writes are
issued through the module-level functions in `core.graph.entity_store`
(none of which commit or rollback), then this module commits on
success and rolls back on any exception. `core.matching.training_data`
likewise never commits — the same transaction covers the graph write
and the training-pair write, satisfying the atomicity requirement.

Index staleness is a simple module-level dirty bit — no incremental
index mutation ships in this feature (`core.matching.indices` has no
incremental update path); `mark_indices_stale()` lets the caller
(feature 12's orchestrator) know a `TokenIndex` / `NgramIndex` /
`EmbeddingIndex` rebuild is due.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from core.graph.approvals import record_approval_decision
from core.graph.audit import log_resolution
from core.graph.entity_store import (
    add_alias,
    add_system_reference,
    create_canonical_entity,
    get_aliases,
    get_canonical_name_and_category,
    upsert_edge,
)
from core.graph.pg import connect as pg_connect
from core.graph.pg import is_available as pg_is_available
from core.ingestion.normalizer import NormalizedEntity
from core.matching.training_data import TrainingPair, store_training_pair
from core.matching.types import Disposition


# ---------------------------------------------------------------------------
# Index staleness signal (module-level dirty bit)
# ---------------------------------------------------------------------------

_indices_stale: bool = False


def mark_indices_stale() -> bool:
    """Return whether the graph has been mutated (a canonical / alias /
    edge write) since the flag was last reset.

    `resolve_match` and `create_new_entity` set this True on a
    successful graph write; `reject_match` never touches it (it makes
    no graph mutation). No incremental index mutation happens here —
    the caller is responsible for rebuilding `TokenIndex` /
    `NgramIndex` / `EmbeddingIndex` from the store when this is True.
    """
    return _indices_stale


def _flag_indices_stale() -> None:
    global _indices_stale
    _indices_stale = True


def reset_indices_stale_flag() -> None:
    """Reset the module-level staleness bit. Test-only helper, mirroring
    `core.matching.llm_fallback.reset_call_budget`."""
    global _indices_stale
    _indices_stale = False


# ---------------------------------------------------------------------------
# entity_pair helpers
# ---------------------------------------------------------------------------

_EMPLOYEE_ID_KEYS: tuple[str, ...] = (
    "employee_id",
    "EmployeeId",
    "EmployeeNumber",
    "employee_number",
)


def _first_employee_id(raw_record: Any) -> Optional[str]:
    """Small allow-listed lookup mirroring `llm_fallback._first_string_value`
    for the employee-id key set. Only used to enrich `entity_pair` for the
    downstream forbidden-token derivation in `training_data.py`."""
    if not isinstance(raw_record, dict):
        return None
    for key in _EMPLOYEE_ID_KEYS:
        value = raw_record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _build_entity_pair(
    conn: sqlite3.Connection,
    entity: NormalizedEntity,
    candidate_canonical_id: str,
    source_call_id: Optional[str],
    tenant_id: Optional[str],
) -> dict[str, Any]:
    """Assemble the raw (unredacted) `entity_pair` payload consumed by
    `store_training_pair` to derive its `call_id` hash and
    forbidden-token set. Never persisted verbatim."""
    candidate_row = get_canonical_name_and_category(conn, candidate_canonical_id, tenant_id)
    candidate_name = candidate_row[0] if candidate_row is not None else None
    return {
        "canonical_id": candidate_canonical_id,
        "incoming_entity_raw": entity.raw_name,
        "incoming_entity_normalized": entity.normalized_name,
        "incoming_email": entity.email,
        "incoming_employee_id": _first_employee_id(entity.raw_record),
        "candidate_canonical_name": candidate_name,
        "candidate_aliases": get_aliases(conn, candidate_canonical_id, tenant_id),
        "source_call_id": source_call_id,
    }


def _stage5_call_id(disposition: Disposition) -> Optional[str]:
    assessment = disposition.llm_assessment
    return assessment.call_id if assessment is not None else None


# ---------------------------------------------------------------------------
# Public Stage 6 entry points
# ---------------------------------------------------------------------------


def resolve_match(
    conn: sqlite3.Connection,
    disposition: Disposition,
    entity: NormalizedEntity,
    canonical_id: str,
    alias_confidence: float,
    source_node: str,
    target_node: str,
    relationship: str,
    source_category: str,
    target_category: str,
    weight: float,
    approved_by: str,
    reasoning_trace: str = "",
    tenant_id: Optional[str] = None,
) -> str:
    """Stage 6 write path for a CONFIRMED match.

    Adds `entity.normalized_name` as an alias of `canonical_id`
    (`source=entity.source`, `category=entity.category`), upserts a
    `relationship` edge `source_node -> target_node` carrying
    `source_category` / `target_category` metadata and `weight`, marks
    indices stale, and stores a positive training pair — written only
    when `disposition.llm_assessment` carries a Stage 5 `call_id` with
    a matching `llm_training_data` row. Idempotent: a second call with
    identical arguments inserts no duplicate alias row and increments
    the existing edge's `approval_count` instead of inserting a second
    edge. Returns `canonical_id`.
    """
    try:
        if pg_is_available():
            # Operational-store audit + approval capture happens before the
            # local graph mutation below, so a connect timeout surfaces
            # here rather than silently skipping past it. Re-drive safety
            # comes from the idempotency of the writes that follow, not
            # from cross-store atomicity.
            store_conn = pg_connect()
            try:
                top = disposition.top_match
                category_pair = f"{source_category}:{target_category}"
                log_resolution(
                    store_conn,
                    canonical_id,
                    entity.raw_name,
                    "CONFIRMED",
                    alias_confidence,
                    top.signal_breakdown if top is not None else {},
                    category_pair,
                    approved_by,
                    tenant_id,
                )
                record_approval_decision(
                    store_conn,
                    source_node,
                    target_node,
                    "approved",
                    top.signal_breakdown if top is not None else {},
                    top.graph_evidence if top is not None else {},
                    category_pair,
                    reasoning_trace,
                    alias_confidence,
                    approved_by,
                    tenant_id,
                )
                store_conn.commit()
            finally:
                store_conn.close()

        add_alias(
            conn,
            canonical_id,
            entity.normalized_name,
            entity.source,
            entity.category,
            alias_confidence,
            tenant_id,
        )
        upsert_edge(
            conn,
            source_node,
            target_node,
            relationship,
            source_category,
            target_category,
            weight,
            approved_by,
            tenant_id,
        )

        entity_pair = _build_entity_pair(
            conn, entity, canonical_id, _stage5_call_id(disposition), tenant_id
        )
        top = disposition.top_match
        pair = TrainingPair(
            entity_pair=entity_pair,
            signal_breakdown=top.signal_breakdown if top is not None else {},
            graph_evidence=top.graph_evidence if top is not None else {},
            category_pair=f"{source_category}:{target_category}",
            disposition="CONFIRMED",
            reasoning_trace=reasoning_trace,
        )
        store_training_pair(conn, pair, tenant_id)

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _flag_indices_stale()
    return canonical_id


def create_new_entity(
    conn: sqlite3.Connection,
    disposition: Disposition,
    entity: NormalizedEntity,
    canonical_name: str,
    entity_type: str,
    entity_category: str,
    confidence: float,
    system_refs: tuple[dict[str, Any], ...],
    approved_by: str,
    related_canonical_ids: tuple[str, ...] = (),
    relationship: str = "SAME_AS",
    weight: float = 1.0,
    reasoning_trace: str = "",
    tenant_id: Optional[str] = None,
) -> str:
    """Stage 6 write path for a NEW canonical entity.

    Creates the canonical node (`create_canonical_entity`), inserts one
    `system_references` row per entry in `system_refs` (each a dict
    with `source`, `category`, `external_id`, and optional
    `external_fields`), initializes a `relationship` edge from the new
    canonical to each id in `related_canonical_ids`, marks indices
    stale, and stores a negative training pair for every candidate in
    `disposition.candidates_ranked` (all of them were rejected in favor
    of the new entity) — LLM-gated per `store_training_pair`. Returns
    the generated `canonical_id`.
    """
    try:
        canonical_id = create_canonical_entity(
            conn, canonical_name, entity_type, entity_category, confidence, tenant_id
        )
        for ref in system_refs:
            add_system_reference(
                conn,
                canonical_id,
                ref["source"],
                ref["category"],
                ref["external_id"],
                ref.get("external_fields"),
                tenant_id,
            )
        for other_canonical_id in related_canonical_ids:
            upsert_edge(
                conn,
                canonical_id,
                other_canonical_id,
                relationship,
                entity_category,
                entity_category,
                weight,
                approved_by,
                tenant_id,
            )

        source_call_id = _stage5_call_id(disposition)
        for candidate in disposition.candidates_ranked:
            candidate_row = get_canonical_name_and_category(conn, candidate.canonical_id, tenant_id)
            entity_pair = {
                "canonical_id": canonical_id,
                "incoming_entity_raw": entity.raw_name,
                "incoming_entity_normalized": entity.normalized_name,
                "incoming_email": entity.email,
                "incoming_employee_id": _first_employee_id(entity.raw_record),
                "rejected_candidate_id": candidate.canonical_id,
                "candidate_canonical_name": candidate_row[0] if candidate_row else None,
                "candidate_aliases": get_aliases(conn, candidate.canonical_id, tenant_id),
                "source_call_id": source_call_id,
            }
            pair = TrainingPair(
                entity_pair=entity_pair,
                signal_breakdown=candidate.signal_breakdown,
                graph_evidence=candidate.graph_evidence,
                category_pair=f"{candidate.category_pair[0]}:{candidate.category_pair[1]}",
                disposition="REJECTED",
                reasoning_trace=reasoning_trace,
            )
            store_training_pair(conn, pair, tenant_id)

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _flag_indices_stale()
    return canonical_id


def reject_match(
    conn: sqlite3.Connection,
    disposition: Disposition,
    entity: NormalizedEntity,
    rejected_canonical_id: str,
    reasoning_trace: str = "",
    tenant_id: Optional[str] = None,
) -> Optional[str]:
    """Stage 6 write path for a REJECTED match.

    No graph mutation — `mark_indices_stale`'s dirty bit is left
    untouched. Stores a hard-negative training pair carrying the full
    signal breakdown for `rejected_canonical_id`, written only when
    `disposition.llm_assessment` carries a Stage 5 `call_id` with a
    matching `llm_training_data` row. Returns the written `call_id`, or
    `None` when no row was written.
    """
    candidate = next(
        (c for c in disposition.candidates_ranked if c.canonical_id == rejected_canonical_id),
        None,
    )
    if candidate is None:
        raise ValueError(
            f"reject_match: {rejected_canonical_id!r} not found in disposition.candidates_ranked"
        )

    try:
        entity_pair = _build_entity_pair(
            conn, entity, rejected_canonical_id, _stage5_call_id(disposition), tenant_id
        )
        pair = TrainingPair(
            entity_pair=entity_pair,
            signal_breakdown=candidate.signal_breakdown,
            graph_evidence=candidate.graph_evidence,
            category_pair=f"{candidate.category_pair[0]}:{candidate.category_pair[1]}",
            disposition="REJECTED",
            reasoning_trace=reasoning_trace,
        )
        call_id = store_training_pair(conn, pair, tenant_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return call_id
