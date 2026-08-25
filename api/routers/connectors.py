"""Nexus Finance — connectors router (feature 16).

Reads and writes the Postgres `connectors` table for the tenant resolved
by `api.middleware.tenant`. `api/` gains exactly one engine in this
feature: Postgres via `core.graph.pg.connect()` — no second driver, no
pooling, no SQLite connection from `api/`.

Neither shipped connector class (`ConnectorInterface` in
`connectors/base.py`) exposes a `sync()` primitive; a manual sync would
compose `authenticate()` + `read_entities()`. This router's
`POST /{provider}/sync` is accepted-and-recorded, not synchronous
ingestion — it stamps `last_sync`/`last_sync_status` and enqueues an
audit row.

An entity-count field is deliberately absent from every payload here — deferred
per Owner Decision 5 / FOLLOW-UP 16-B
(`features/infrastructure/connectors-audit-infra.md`): the data lives in
the SQLite graph store and `api/` has no read path to it.

The database connection is obtained through the `get_conn` dependency
(rather than calling `core.graph.pg.connect()` directly in the handler
body) so tests can override it independently of monkeypatching the
connection factory itself — the seam `tests/test_audit.py` uses to prove
`AuditMiddleware` never touches the database on the request path.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row

from core.graph import pg

router = APIRouter(prefix="/connectors", tags=["connectors"])


def get_conn() -> Iterator["pg.psycopg.Connection"]:
    conn = pg.connect()
    try:
        yield conn
    finally:
        conn.close()


def _fetch_connector(conn, tenant_id: str, provider: str) -> Optional[Dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT provider, category, last_sync, last_sync_status, last_sync_error "
            "FROM connectors WHERE tenant_id = %s AND provider = %s",
            (tenant_id, provider),
        )
        return cur.fetchone()


@router.get("/")
def list_connectors(request: Request, conn=Depends(get_conn)) -> list:
    tenant_id = request.state.tenant_id
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT provider, category, last_sync, last_sync_status, last_sync_error "
            "FROM connectors WHERE tenant_id = %s",
            (tenant_id,),
        )
        rows = cur.fetchall()

    return [
        {
            "provider": row["provider"],
            "category": row["category"],
            "connected": True,
            "last_sync": row["last_sync"],
            "last_sync_status": row["last_sync_status"],
            "last_sync_error": row["last_sync_error"],
        }
        for row in rows
    ]


@router.post("/{provider}/sync", status_code=202)
def sync_connector(provider: str, request: Request, conn=Depends(get_conn)) -> JSONResponse:
    tenant_id = request.state.tenant_id
    row = _fetch_connector(conn, tenant_id, provider)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown connector: {provider!r}")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE connectors SET last_sync = now(), last_sync_status = 'accepted' "
            "WHERE tenant_id = %s AND provider = %s",
            (tenant_id, provider),
        )
    conn.commit()

    request.state.audit_category = row["category"]
    return JSONResponse(
        status_code=202, content={"provider": provider, "status": "accepted"}
    )


@router.get("/{provider}/status")
def connector_status(provider: str, request: Request, conn=Depends(get_conn)) -> dict:
    tenant_id = request.state.tenant_id
    row = _fetch_connector(conn, tenant_id, provider)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown connector: {provider!r}")

    request.state.audit_category = row["category"]
    return {
        "last_sync": row["last_sync"],
        "last_sync_status": row["last_sync_status"],
        "last_sync_error": row["last_sync_error"],
    }
