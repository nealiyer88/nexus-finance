"""Tenant provisioning for the Postgres operational store (feature 10c).

`resolve_or_create_tenant` is the one caller-facing contract fixed by the
brief: `str` in, `str` out, `name`/`slug` required with no defaults (both
columns are `NOT NULL`, `slug` additionally `UNIQUE`). A `slug` collision
on a *different* row surfaces as whatever `psycopg` raises for the unique
violation — never swallowed, never retried, and the message carries no
connection information.

`resolve_tenant_for_write` is the shared NULL-tenant fallback every 10c
writer uses: `tenant_id is None` maps to 10a's `BOOTSTRAP_TENANT_ID`
directly (that row is seeded by the tenant-bootstrap migration); a
syntactically valid UUID string is provisioned via `resolve_or_create_tenant`
using the tenant id itself as both `name` and `slug`, since a bare writer
call carries no richer tenant metadata to provision with. Slug
de-duplication for that synthetic slug is out of scope — a caller wiring a
real tenant onboarding path should call `resolve_or_create_tenant` directly
with real values.

A `tenant_id` that is not `None` and not a syntactically valid UUID (the
`tenants.id` / `canonical_entities.tenant_id` column type on the Postgres
side) also falls back to `BOOTSTRAP_TENANT_ID` rather than raising —
`core.matching.engine`'s `MatchContext.tenant_id` is a free-form app-level
scoping string on the SQLite side with no format constraint, so a caller
upstream of this feature may legitimately pass a non-UUID value; this
writer path degrades to the shared bootstrap tenant instead of failing the
whole Stage 6 write for a formatting mismatch it doesn't own.

All connections arrive already open, courtesy of `core.graph.pg.connect()`
— this module never imports the driver to open one, never reads the
environment, and never builds a DSN from parts.
"""

from __future__ import annotations

import uuid
from typing import Optional

from core.graph.pg import BOOTSTRAP_TENANT_ID


def resolve_or_create_tenant(conn, tenant_id: str, name: str, slug: str) -> str:
    """Ensure a `tenants` row exists for `tenant_id` and return its id.

    `INSERT ... ON CONFLICT (id) DO NOTHING` plus a `SELECT`. Both `name`
    and `slug` are required — the columns backing them are `NOT NULL`.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, name, slug) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (tenant_id, name, slug),
        )
        cur.execute("SELECT id FROM tenants WHERE id = %s", (tenant_id,))
        row = cur.fetchone()

    if row is None:
        # Only reachable if the insert above failed for a reason other
        # than the id already existing (e.g. a slug collision surfaced
        # by the driver before this point) — the caller sees that error
        # directly since we never catch it above.
        raise RuntimeError(f"tenant {tenant_id!r} not found after provisioning attempt")

    return str(row[0])


def resolve_tenant_for_write(conn, tenant_id: Optional[str]) -> str:
    """The NULL-tenant fallback shared by every 10c writer.

    `None`, or a value that is not a syntactically valid UUID, maps to the
    seeded bootstrap tenant id. A valid UUID string is provisioned
    (idempotently) via `resolve_or_create_tenant`, using the id itself as
    both `name` and `slug`.
    """
    if tenant_id is None:
        return BOOTSTRAP_TENANT_ID
    try:
        uuid.UUID(str(tenant_id))
    except ValueError:
        return BOOTSTRAP_TENANT_ID
    return resolve_or_create_tenant(conn, tenant_id, name=str(tenant_id), slug=str(tenant_id))
