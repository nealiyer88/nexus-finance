"""Reconcile SQLite graph mutations against the Postgres audit trail
(feature 10c).

The invariant checked is COVERAGE, not count equality: for a given tenant
and a watermark timestamp (default: the tenant's earliest `audit_log
.created_at`), every distinct canonical identifier touched by a SQLite
`entity_aliases` / `entity_edges` row at or after the watermark must have
at least one corresponding `audit_log` row. SQLite alias/edge rows carry
no `tenant_id` of their own — tenant scope is derived by joining to
`canonical_entities.tenant_id`, and a SQLite row whose `tenant_id` is NULL
is treated as belonging to the bootstrap tenant for this comparison (a
plain equality join would otherwise select nothing and vacuously "pass").

NON-VACUITY GATE: before any conclusion is drawn, the script asserts the
collected SQLite identifier set is non-empty. An empty set is a failure
with an explicit message — never a silent success and never a zero exit.

Exit code is non-zero ONLY on the uncovered-mutation direction (a SQLite
identifier with no covering audit row) or on the non-vacuity gate. The
reverse direction — an audit row with no matching graph mutation — is
expected and benign, reported as an informational line only.

`DATABASE_URL` is never printed on any path. All Postgres access goes
through `core.graph.pg.connect()`.
"""

from __future__ import annotations

import argparse
import dataclasses
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Optional

from core.graph import pg


class ReconcileError(RuntimeError):
    """Raised when the non-vacuity gate fails."""


@dataclasses.dataclass(frozen=True)
class ReconcileResult:
    sqlite_ids: frozenset
    pg_ids: frozenset
    uncovered: tuple
    informational: tuple


def _normalize_ts(value) -> datetime:
    """Normalize a SQLite TEXT timestamp or a Postgres TIMESTAMPTZ value
    to a UTC-aware `datetime` so the two engines' timestamps compare."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1)
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            dt = datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _default_watermark(pg_conn, tenant_id: str) -> Optional[datetime]:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT MIN(created_at) FROM audit_log WHERE tenant_id = %s",
            (tenant_id,),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return _normalize_ts(row[0])


def _sqlite_tenant_scoped_ids(
    sqlite_conn: sqlite3.Connection, tenant_id: str, watermark: Optional[datetime]
) -> set:
    is_bootstrap = tenant_id == pg.BOOTSTRAP_TENANT_ID
    ids: set = set()

    alias_rows = sqlite_conn.execute(
        """
        SELECT ea.canonical_id, ea.created_at
          FROM entity_aliases ea
          JOIN canonical_entities ce ON ce.canonical_id = ea.canonical_id
         WHERE ce.tenant_id = ? OR (? AND ce.tenant_id IS NULL)
        """,
        (tenant_id, is_bootstrap),
    ).fetchall()
    for canonical_id, created_at in alias_rows:
        if watermark is None or _normalize_ts(created_at) >= watermark:
            ids.add(str(canonical_id))

    edge_rows = sqlite_conn.execute(
        """
        SELECT ee.source_node, ee.target_node, ee.created_at
          FROM entity_edges ee
          JOIN canonical_entities src ON src.canonical_id = ee.source_node
         WHERE src.tenant_id = ? OR (? AND src.tenant_id IS NULL)
        """,
        (tenant_id, is_bootstrap),
    ).fetchall()
    for source_node, target_node, created_at in edge_rows:
        if watermark is None or _normalize_ts(created_at) >= watermark:
            ids.add(str(source_node))
            ids.add(str(target_node))

    return ids


def _pg_covered_ids(pg_conn, tenant_id: str, watermark: Optional[datetime]) -> set:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT resource_id, created_at FROM audit_log "
            "WHERE tenant_id = %s AND resource_id IS NOT NULL",
            (tenant_id,),
        )
        rows = cur.fetchall()

    ids: set = set()
    for resource_id, created_at in rows:
        if watermark is None or _normalize_ts(created_at) >= watermark:
            ids.add(str(resource_id))
    return ids


def reconcile(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    tenant_id: str,
    watermark: Optional[datetime] = None,
) -> ReconcileResult:
    """Compute the coverage comparison for one tenant. Raises
    `ReconcileError` when the non-vacuity gate fails."""
    if watermark is None:
        watermark = _default_watermark(pg_conn, tenant_id)

    sqlite_ids = _sqlite_tenant_scoped_ids(sqlite_conn, tenant_id, watermark)
    if not sqlite_ids:
        raise ReconcileError(
            f"non-vacuity gate failed: no SQLite canonical identifiers found for "
            f"tenant {tenant_id!r} at or after the watermark"
        )

    pg_ids = _pg_covered_ids(pg_conn, tenant_id, watermark)

    uncovered = tuple(sorted(sqlite_ids - pg_ids))
    informational = tuple(sorted(pg_ids - sqlite_ids))

    return ReconcileResult(
        sqlite_ids=frozenset(sqlite_ids),
        pg_ids=frozenset(pg_ids),
        uncovered=uncovered,
        informational=informational,
    )


def main(argv: Optional[list] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        description="Reconcile SQLite graph mutations against the Postgres audit trail."
    )
    parser.add_argument("--sqlite-path", required=True, help="Path to the SQLite graph-store file.")
    parser.add_argument("--tenant-id", required=True, help="Tenant id to reconcile.")
    parser.add_argument(
        "--watermark",
        default=None,
        help="ISO-8601 UTC watermark; defaults to the tenant's earliest audit_log row.",
    )
    args = parser.parse_args(argv)

    watermark = _normalize_ts(args.watermark) if args.watermark else None

    sqlite_conn = sqlite3.connect(args.sqlite_path)
    pg_conn = pg.connect()
    try:
        try:
            result = reconcile(sqlite_conn, pg_conn, args.tenant_id, watermark)
        except ReconcileError as exc:
            print(str(exc))
            return 1

        print(f"sqlite identifiers checked: {len(result.sqlite_ids)}")
        print(f"postgres identifiers checked: {len(result.pg_ids)}")

        for identifier in result.uncovered:
            print(f"uncovered: {identifier}")
        for identifier in result.informational:
            print(f"informational: audit row with no graph mutation for {identifier}")

        return 1 if result.uncovered else 0
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
