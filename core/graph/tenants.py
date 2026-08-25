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

A `tenant_id` that is PRESENT but not a syntactically valid UUID (the
`tenants.id` / `canonical_entities.tenant_id` column type on the Postgres
side) raises `ValueError` — it is never degraded to `BOOTSTRAP_TENANT_ID`.
Silently bucketing a malformed-but-present tenant identifier into the
shared bootstrap tenant would file one customer's audit and approval rows
under another tenant, and an audit trail whose tenant attribution can be
silently wrong is worse than none at all. Only the absence of a tenant
(`None`) is a sanctioned fallback. The raised message names the offending
value and the expected format and carries no connection or credential
material.

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

    `None` — the absence of a tenant — maps to the seeded bootstrap tenant
    id. A valid UUID string is provisioned (idempotently) via
    `resolve_or_create_tenant`, using the id itself as both `name` and
    `slug`. A present-but-malformed tenant identifier raises `ValueError`
    rather than degrading to the bootstrap tenant: writing one tenant's
    rows under another's id is a tenant-isolation failure, not a
    formatting inconvenience.
    """
    if tenant_id is None:
        return BOOTSTRAP_TENANT_ID
    try:
        uuid.UUID(str(tenant_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(
            "tenant_id must be None or a syntactically valid UUID string "
            f"(the tenants.id column type); got {tenant_id!r}"
        ) from exc
    return resolve_or_create_tenant(conn, tenant_id, name=str(tenant_id), slug=str(tenant_id))
