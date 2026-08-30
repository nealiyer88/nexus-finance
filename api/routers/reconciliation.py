"""Nexus Finance — reconciliation router (feature 15).

Exposes `core.reconciliation.ar` over HTTP. Opens SQLite only through
`core.matching.pending_store.get_connection` — never constructs a
`sqlite3.Connection` of its own. Tenant handling mirrors
`api/routers/entities.py`: it reads `request.state.tenant_id`, stamped
by `api.middleware.tenant`, and parses no tenant parameter of its own.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from core.matching.pending_store import get_connection
from core.reconciliation.ar import get_ar_detail, get_ar_report

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


@router.get("/ar")
def get_ar_report_route(request: Request) -> dict:
    tenant_id = request.state.tenant_id
    with get_connection() as conn:
        items = get_ar_report(conn, tenant_id)
    return {"items": items}


@router.get("/ar/{canonical_id}")
def get_ar_detail_route(canonical_id: str, request: Request) -> dict:
    tenant_id = request.state.tenant_id
    with get_connection() as conn:
        detail = get_ar_detail(conn, canonical_id, tenant_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="canonical entity not found")
    return detail
