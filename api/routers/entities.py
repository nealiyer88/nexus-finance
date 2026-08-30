"""Nexus Finance — entities router (feature 14).

Exposes the canonical entity graph for the Overview dashboard's KPI
cards and the Entity Registry Browser: aggregate stats, a paginated /
filtered / fuzzy-searched entity list, and a per-entity detail view
(aliases grouped by source category, system references, graph edges).

This module opens SQLite only through `core.matching.pending_store.
get_connection` — it never constructs a `sqlite3.Connection` of its
own. Tenant handling mirrors `api/routers/approvals.py`: it reads
`request.state.tenant_id`, stamped by `api.middleware.tenant`, and
parses no header or environment variable of its own.

The module-level functions below (`compute_stats`, `list_entities`,
`get_entity_detail`) are the single source of truth for these
computations — the HTTP routes are thin wrappers around them, and the
Overview / Entity Graph dashboard pages import and call them directly
(the same pattern `dashboard/pages/approval_queue.py` uses for
`api/routers/approvals.py`'s decision functions), so the API and the
dashboard can never compute a KPI two different ways.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from core.graph.entity_search import search_canonical_ids
from core.graph.entity_store import (
    count_canonical_entities,
    count_cross_category_entities,
    get_aliases_grouped_by_category,
    get_canonical_entity,
    get_edges_for_canonical,
    get_system_references,
    list_canonical_entities,
    list_entities_for_search,
)
from core.matching.disposition import AUTO_APPROVE_THRESHOLD
from core.matching.pending_store import get_connection

router = APIRouter(prefix="/entities", tags=["entities"])

# Auto-Match Rate trailing window (Open Question 1 in the feature
# brief: no resolution-event table records a real auto-approve
# disposition, so this is the approved in-scope proxy — confidence
# >= AUTO_APPROVE_THRESHOLD over canonicals created within this window).
AUTO_MATCH_WINDOW_DAYS: int = 90

DEFAULT_LIST_LIMIT: int = 50


# ---------------------------------------------------------------------------
# Computations — shared by the HTTP routes and the dashboard pages.
# ---------------------------------------------------------------------------


def _count_pending(conn: Any, tenant_id: Optional[str]) -> int:
    """`COUNT(*)` of `pending_decisions` rows with `status = 'pending'`,
    tenant-scoped. Not `len(list_pending(...))` — that helper defaults to
    `limit: int = 100` and undercounts past that page."""
    if tenant_id is None:
        row = conn.execute(
            "SELECT COUNT(*) FROM pending_decisions WHERE status = 'pending'"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM pending_decisions WHERE status = 'pending' AND tenant_id = ?",
            (tenant_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def compute_stats(conn: Any, tenant_id: Optional[str] = None) -> dict[str, Any]:
    """Compute the four Overview KPI values (see `.claude/rules/
    01-nexus-finance-v1.md` and the feature brief's KPI Definitions
    table). Field names deliberately avoid the literal `entity` +
    `_count` (a shipped guard test forbids it) — `resolved_entities`
    stands in for "Entities Resolved"."""
    resolved_entities = count_canonical_entities(conn, tenant_id=tenant_id)

    # `canonical_entities.created_at` is stamped by SQLite's
    # CURRENT_TIMESTAMP, which renders "YYYY-MM-DD HH:MM:SS" (space
    # separator, no offset, no fractional seconds) — the comparison
    # string below is formatted to match byte-for-byte, since the
    # comparison in `count_canonical_entities` is a plain text `>=`.
    window_start = (
        datetime.now(timezone.utc) - timedelta(days=AUTO_MATCH_WINDOW_DAYS)
    ).strftime("%Y-%m-%d %H:%M:%S")
    window_total = count_canonical_entities(
        conn, tenant_id=tenant_id, created_after=window_start
    )
    window_auto = count_canonical_entities(
        conn,
        tenant_id=tenant_id,
        min_confidence=AUTO_APPROVE_THRESHOLD,
        created_after=window_start,
    )
    auto_match_rate = (window_auto / window_total) if window_total else 0.0

    pending_approvals = _count_pending(conn, tenant_id)

    cross_category_numerator = count_cross_category_entities(conn, tenant_id=tenant_id)
    cross_category_coverage = (
        (cross_category_numerator / resolved_entities) if resolved_entities else 0.0
    )

    return {
        "resolved_entities": resolved_entities,
        "auto_match_rate": auto_match_rate,
        "pending_approvals": pending_approvals,
        "cross_category_coverage": cross_category_coverage,
    }


def list_entities(
    conn: Any,
    tenant_id: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_category: Optional[str] = None,
    min_confidence: Optional[float] = None,
    max_confidence: Optional[float] = None,
    source_category: Optional[str] = None,
    q: Optional[str] = None,
    limit: Optional[int] = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Filtered, optionally fuzzy-searched, paginated entity list for
    the Entity Registry Browser table.

    Filters run at the SQL layer (`entity_store.list_canonical_entities`,
    no limit — the full filtered set); when `q` is given, RapidFuzz
    (`core.graph.entity_search`) further narrows and ranks that set.
    Pagination is applied last, over the final filtered/ranked list.
    """
    rows = list_canonical_entities(
        conn,
        tenant_id=tenant_id,
        entity_type=entity_type,
        entity_category=entity_category,
        min_confidence=min_confidence,
        max_confidence=max_confidence,
        source_category=source_category,
    )

    if q is not None and q.strip():
        candidates = list_entities_for_search(conn, tenant_id=tenant_id)
        matched_ids = search_canonical_ids(candidates, q)
        rank = {canonical_id: idx for idx, canonical_id in enumerate(matched_ids)}
        rows = [row for row in rows if row["canonical_id"] in rank]
        rows.sort(key=lambda row: rank[row["canonical_id"]])

    total = len(rows)
    if limit is None:
        page = rows[offset:]
    else:
        page = rows[offset : offset + limit]

    return {"items": page, "total": total, "limit": limit, "offset": offset}


def get_entity_detail(
    conn: Any,
    canonical_id: str,
    tenant_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Entity detail: the canonical row plus aliases grouped by
    `entity_aliases.category`, system references, and graph edges. None
    if `canonical_id` is absent or out of tenant scope."""
    entity = get_canonical_entity(conn, canonical_id, tenant_id)
    if entity is None:
        return None
    return {
        **entity,
        "aliases": get_aliases_grouped_by_category(conn, canonical_id, tenant_id),
        "system_references": get_system_references(conn, canonical_id, tenant_id),
        "edges": get_edges_for_canonical(conn, canonical_id, tenant_id),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/stats")
def get_stats_route(request: Request) -> dict:
    tenant_id = request.state.tenant_id
    with get_connection() as conn:
        return compute_stats(conn, tenant_id)


@router.get("/")
def list_entities_route(
    request: Request,
    entity_type: Optional[str] = None,
    entity_category: Optional[str] = None,
    min_confidence: Optional[float] = None,
    max_confidence: Optional[float] = None,
    source_category: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> dict:
    tenant_id = request.state.tenant_id
    with get_connection() as conn:
        return list_entities(
            conn,
            tenant_id,
            entity_type=entity_type,
            entity_category=entity_category,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            source_category=source_category,
            q=q,
            limit=limit,
            offset=offset,
        )


@router.get("/{canonical_id}")
def get_entity_route(canonical_id: str, request: Request) -> dict:
    tenant_id = request.state.tenant_id
    with get_connection() as conn:
        detail = get_entity_detail(conn, canonical_id, tenant_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return detail
