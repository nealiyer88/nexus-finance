"""Nexus Finance — audit middleware (feature 16).

Every request matching a route registered from `api/routers/*` produces
exactly one `audit_log` row; `GET /health` is the single documented
exemption. Writes are entirely off the request path: `AuditMiddleware`
builds a row dict and hands it to a module-level `AuditQueue` via
`enqueue(row)`, then returns the response immediately. A single daemon
worker thread drains the queue and performs the INSERTs — the request
handler makes zero database round trips for auditing.

This is a *separate* writer from feature 10c's `core.graph.audit.log_resolution`
(the Stage 6 resolution audit trail) — same table, different rows. Every
row this module writes sets `resource = "connectors"` and a `resource_id`
that is a provider name (never a canonical identifier), per the
audit-row namespacing rule in
`features/infrastructure/connectors-audit-infra.md`, so 10c's
reconciliation script can exclude them by filtering on `resource`.

Append-only: this module issues an INSERT statement only against
`audit_log` — it never modifies or removes a row it already wrote.

Error taxonomy — exactly three branches, and only the first drops:
  1. Queue full: `enqueue` uses `put_nowait`; on `queue.Full` the row is
     dropped and a `WARNING` logged. The only sanctioned drop.
  2. Unprovisioned tenant (FK violation, `psycopg.errors.ForeignKeyViolation`
     — a `psycopg.IntegrityError`, a sibling of `psycopg.DataError` under
     `psycopg.DatabaseError`): `ERROR` naming the tenant_id, pointing at
     feature 10c's tenant-bootstrap step; `failed_writes` incremented; no
     drop, no fallback, no create-or-resolve call.
  3. Malformed tenant identifier (`psycopg.DataError` — the parent class
     for the driver's invalid-input-syntax error, e.g.
     `InvalidTextRepresentation` — or the built-in `ValueError`, matching
     the exception type 10c's shared tenant resolver in
     `core/graph/tenants.py` raises for a present-but-invalid identifier):
     handled exactly as loudly as branch 2.

The worker's outermost handler is broad enough that no exception of any
class escapes the daemon thread; anything unmatched is logged at `ERROR`
and counted, never swallowed at `WARNING`.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Any, Dict, Optional

import psycopg
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from core.graph import pg

logger = logging.getLogger(__name__)

# Bounded so a stalled worker cannot grow unbounded memory; overflow is the
# one sanctioned drop (branch 1 above).
_QUEUE_MAXSIZE = 10_000


class AuditQueue:
    """In-process queue plus a single daemon worker draining it into
    Postgres `audit_log`. Public surface: `enqueue`, `drain`,
    `failed_writes`, `start_worker`, `stop_worker`.
    """

    def __init__(self, maxsize: int = _QUEUE_MAXSIZE) -> None:
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=maxsize)
        self.failed_writes = 0
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def enqueue(self, row: Dict[str, Any]) -> None:
        """Non-blocking enqueue. Never raises into the request path."""
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            logger.warning("audit_log row dropped: queue full")

    def _write_row(self, conn: "psycopg.Connection", row: Dict[str, Any]) -> None:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_log (
                        tenant_id, actor_id, action, resource, resource_id, category, diff
                    ) VALUES (%s, NULL, %s, %s, %s, %s, %s)
                    """,
                    (
                        row["tenant_id"],
                        row["action"],
                        row["resource"],
                        row.get("resource_id"),
                        row.get("category"),
                        json.dumps(row.get("diff")),
                    ),
                )
            conn.commit()
        except psycopg.errors.ForeignKeyViolation:
            conn.rollback()
            self.failed_writes += 1
            logger.error(
                "audit_log INSERT failed: tenant_id %r has no tenants row "
                "(see feature 10c's tenant-bootstrap provisioning step)",
                row.get("tenant_id"),
            )
        except (psycopg.DataError, ValueError) as exc:
            conn.rollback()
            self.failed_writes += 1
            logger.error(
                "audit_log INSERT failed: malformed tenant identifier %r (%s)",
                row.get("tenant_id"),
                exc,
            )
        except Exception:  # noqa: BLE001 - outermost catch-all, must never escape
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            self.failed_writes += 1
            logger.error("audit_log INSERT failed: unexpected error", exc_info=True)

    def drain(self) -> None:
        """Synchronously write all currently pending rows and return."""
        if not pg.is_available():
            # Nothing to write to; still drain the queue so tests observing
            # `failed_writes` deterministically without a database configured
            # don't hang. Each row is counted as failed.
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                self.failed_writes += 1
                logger.error("audit_log INSERT failed: no Postgres configured")
            return

        conn = pg.connect()
        try:
            while True:
                try:
                    row = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._write_row(conn, row)
        finally:
            conn.close()

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            self.drain()
            self._stop.wait(timeout=0.1)

    def start_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self._worker = threading.Thread(
                target=self._worker_loop, name="audit-queue-worker", daemon=True
            )
            self._worker.start()

    def stop_worker(self) -> None:
        with self._lock:
            self._stop.set()
            if self._worker is not None:
                self._worker.join(timeout=1)
            self._worker = None


# Module-level queue, shared by the middleware and every test using
# `AuditQueue.drain()`.
audit_queue = AuditQueue()

# Action map — fixed per features/infrastructure/connectors-audit-infra.md.
# Keys are (method, path_template) as FastAPI registers them.
ACTION_MAP: Dict[tuple, Dict[str, Optional[str]]] = {
    ("GET", "/connectors/"): {"action": "connector.list", "resource": "connectors"},
    ("POST", "/connectors/{provider}/sync"): {
        "action": "connector.sync",
        "resource": "connectors",
    },
    ("GET", "/connectors/{provider}/status"): {
        "action": "connector.status",
        "resource": "connectors",
    },
}

# Routes exempt from audit logging.
EXEMPT_PATHS = {("GET", "/health")}


class AuditMiddleware(BaseHTTPMiddleware):
    """Enqueues one `audit_log` row per matched, non-exempt route.

    Performs no database round trip on the request path — building the row
    dict and calling `audit_queue.enqueue()` are the only operations here.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        route = request.scope.get("route")
        if route is None:
            return response

        path_template = getattr(route, "path", None)
        method = request.method
        if (method, path_template) in EXEMPT_PATHS:
            return response

        mapping = ACTION_MAP.get((method, path_template))
        if mapping is None:
            return response

        provider = request.path_params.get("provider")
        resource_id = provider if provider is not None else None
        category = getattr(request.state, "audit_category", None)

        row = {
            "tenant_id": getattr(request.state, "tenant_id", None),
            "action": mapping["action"],
            "resource": mapping["resource"],
            "resource_id": resource_id,
            "category": category,
            "diff": None,
        }
        audit_queue.enqueue(row)
        return response
