"""Read-only interface to the SQLite graph store for matcher Stages 1–2.

Exposes deterministic-match anchor lookups (alias_exact, email, employee_id),
plus a small helper for the Stage 2d intra-system filter. All functions are
module-level — no class wrapper, no shared state, no write path. Stage 6
(resolution / graph update) will add write functions to this same module.

Tenant scoping: every read takes `tenant_id: Optional[str] = None`. When
`None`, no WHERE filter is applied (V1 single-tenant SQLite default —
`canonical_entities.tenant_id` is nullable and fixtures load with NULL).
When set, queries filter on `canonical_entities.tenant_id`; alias and
system_reference tables join through `canonical_id`.

`lookup_email` / `lookup_employee_id` read `system_references.external_fields`
(a JSON TEXT column) and filter in Python. The V1 schema has no first-class
email or employee_id columns; reading JSON in Python is acceptable at V1
scale (<500 canonicals per tenant).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional


def lookup_alias_exact(
    conn: sqlite3.Connection,
    normalized_value: str,
    tenant_id: Optional[str] = None,
) -> list[tuple[str, float]]:
    """Return (canonical_id, confidence) pairs whose alias value or canonical
    name exactly equals `normalized_value`.

    A `canonical_entities.canonical_name` row is treated as a seed alias
    with confidence 1.0. When the same `canonical_id` is hit via both the
    alias path and the canonical-name seed path, the entry is deduped and
    the MAX confidence is kept (so a single-canonical match never looks
    like a collision to the caller). The returned list is sorted by
    `canonical_id` for deterministic ordering. Multiple distinct
    canonical_ids indicate a true collision and are all returned;
    callers must decide whether to resolve or fall through.
    """
    best: dict[str, float] = {}

    if tenant_id is None:
        rows = conn.execute(
            """
            SELECT a.canonical_id, a.confidence
              FROM entity_aliases AS a
             WHERE a.value = ?
            """,
            (normalized_value,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT a.canonical_id, a.confidence
              FROM entity_aliases AS a
              JOIN canonical_entities AS c ON c.canonical_id = a.canonical_id
             WHERE a.value = ?
               AND c.tenant_id = ?
            """,
            (normalized_value, tenant_id),
        ).fetchall()

    for cid, conf in rows:
        conf_f = float(conf) if conf is not None else 1.0
        if cid not in best or conf_f > best[cid]:
            best[cid] = conf_f

    if tenant_id is None:
        seed_rows = conn.execute(
            "SELECT canonical_id FROM canonical_entities WHERE canonical_name = ?",
            (normalized_value,),
        ).fetchall()
    else:
        seed_rows = conn.execute(
            """
            SELECT canonical_id
              FROM canonical_entities
             WHERE canonical_name = ?
               AND tenant_id = ?
            """,
            (normalized_value, tenant_id),
        ).fetchall()

    for (cid,) in seed_rows:
        if cid not in best or 1.0 > best[cid]:
            best[cid] = 1.0

    return [(cid, best[cid]) for cid in sorted(best)]


def lookup_email(
    conn: sqlite3.Connection,
    email: str,
    tenant_id: Optional[str] = None,
) -> list[str]:
    """Return canonical_ids whose system_references payload carries this email
    (case-insensitive). Multiple entries indicate a shared inbox — Stage 1
    declines on collision.
    """
    needle = email.strip().lower()
    if not needle:
        return []

    # LIKE filter is a perf hint; depends on json.dumps default ASCII quoting. Python JSON parse below is the authoritative match.
    if tenant_id is None:
        rows = conn.execute(
            """
            SELECT s.canonical_id, s.external_fields
              FROM system_references AS s
             WHERE s.external_fields LIKE ?
            """,
            ('%"email"%',),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT s.canonical_id, s.external_fields
              FROM system_references AS s
              JOIN canonical_entities AS c ON c.canonical_id = s.canonical_id
             WHERE s.external_fields LIKE ?
               AND c.tenant_id = ?
            """,
            ('%"email"%', tenant_id),
        ).fetchall()

    seen: list[str] = []
    deduped: set[str] = set()
    for cid, fields_json in rows:
        if not fields_json:
            continue
        try:
            payload = json.loads(fields_json)
        except (TypeError, ValueError):
            continue
        candidate_email = payload.get("email") if isinstance(payload, dict) else None
        if not isinstance(candidate_email, str):
            continue
        if candidate_email.strip().lower() == needle:
            if cid not in deduped:
                seen.append(cid)
                deduped.add(cid)
    return seen


def lookup_employee_id(
    conn: sqlite3.Connection,
    employee_id: str,
    tenant_id: Optional[str] = None,
) -> list[str]:
    """Return canonical_ids whose system_references payload carries this
    employee_id. Exact string match (case-sensitive).
    """
    needle = employee_id.strip()
    if not needle:
        return []

    if tenant_id is None:
        rows = conn.execute(
            """
            SELECT s.canonical_id, s.external_fields
              FROM system_references AS s
             WHERE s.external_fields LIKE ?
            """,
            ('%"employee_id"%',),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT s.canonical_id, s.external_fields
              FROM system_references AS s
              JOIN canonical_entities AS c ON c.canonical_id = s.canonical_id
             WHERE s.external_fields LIKE ?
               AND c.tenant_id = ?
            """,
            ('%"employee_id"%', tenant_id),
        ).fetchall()

    seen: list[str] = []
    deduped: set[str] = set()
    for cid, fields_json in rows:
        if not fields_json:
            continue
        try:
            payload = json.loads(fields_json)
        except (TypeError, ValueError):
            continue
        candidate_eid = payload.get("employee_id") if isinstance(payload, dict) else None
        if candidate_eid is None:
            continue
        if str(candidate_eid).strip() == needle:
            if cid not in deduped:
                seen.append(cid)
                deduped.add(cid)
    return seen


def get_system_refs(
    conn: sqlite3.Connection,
    canonical_id: str,
) -> list[tuple[str, str]]:
    """Return `(source, external_id)` pairs for every system_references row
    on this canonical. Used by the Stage 2d intra-system filter."""
    rows = conn.execute(
        """
        SELECT source, external_id
          FROM system_references
         WHERE canonical_id = ?
        """,
        (canonical_id,),
    ).fetchall()
    return [(source, external_id) for source, external_id in rows]


def get_entity_category(
    conn: sqlite3.Connection,
    canonical_id: str,
) -> Optional[str]:
    """Return `entity_category` ('organization' | 'person') for a canonical,
    or None if absent. Used by Stage 1's email/employee_id person-only
    discrimination."""
    row = conn.execute(
        "SELECT entity_category FROM canonical_entities WHERE canonical_id = ?",
        (canonical_id,),
    ).fetchone()
    if row is None:
        return None
    return row[0]
