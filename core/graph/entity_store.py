"""Read/write interface to the SQLite graph store for matcher Stages 1–2 and 6.

Exposes deterministic-match anchor lookups (alias_exact, email, employee_id),
plus a small helper for the Stage 2d intra-system filter. All functions are
module-level — no class wrapper, no shared state. Stage 6 (resolution /
graph update) write functions are appended below the Stage 1–4 read
functions; none of them call `conn.commit()` or `conn.rollback()` — the
transaction boundary is owned exclusively by `core.graph.resolution`.

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
import uuid
from datetime import datetime
from typing import Any, Optional


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


# ---------------------------------------------------------------------------
# Stage 3 reads (pairwise scoring)
# ---------------------------------------------------------------------------


def get_aliases(
    conn: sqlite3.Connection,
    canonical_id: str,
    tenant_id: Optional[str] = None,
) -> list[str]:
    """Return every `entity_aliases.value` for this canonical.

    The canonical's own `canonical_name` is NOT included — Stage 3
    scores it separately as part of the weighted string-metric sum.
    The V1 canonical-write convention seeds a `value == canonical_name`
    alias (rules §3 example), so the exclusion is enforced at the SQL
    layer via a JOIN against `canonical_entities` and a `value !=
    canonical_name` filter. Tenant-scoped when `tenant_id` is set.
    """
    if tenant_id is None:
        rows = conn.execute(
            """
            SELECT a.value
              FROM entity_aliases AS a
              JOIN canonical_entities AS c ON c.canonical_id = a.canonical_id
             WHERE a.canonical_id = ?
               AND a.value != c.canonical_name
            """,
            (canonical_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT a.value
              FROM entity_aliases AS a
              JOIN canonical_entities AS c ON c.canonical_id = a.canonical_id
             WHERE a.canonical_id = ?
               AND c.tenant_id = ?
               AND a.value != c.canonical_name
            """,
            (canonical_id, tenant_id),
        ).fetchall()
    return [value for (value,) in rows if value]


def get_canonical_name_and_category(
    conn: sqlite3.Connection,
    canonical_id: str,
    tenant_id: Optional[str] = None,
) -> Optional[tuple[str, str, str]]:
    """Return `(canonical_name, entity_category, entity_type)` for
    `canonical_id`, or None if absent (or out of tenant scope).
    """
    if tenant_id is None:
        row = conn.execute(
            """
            SELECT canonical_name, entity_category, entity_type
              FROM canonical_entities
             WHERE canonical_id = ?
            """,
            (canonical_id,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT canonical_name, entity_category, entity_type
              FROM canonical_entities
             WHERE canonical_id = ?
               AND tenant_id = ?
            """,
            (canonical_id, tenant_id),
        ).fetchone()
    if row is None:
        return None
    return (row[0], row[1], row[2])


def _neighbors(
    conn: sqlite3.Connection,
    canonical_id: str,
    tenant_id: Optional[str],
) -> set[str]:
    """Return the set of canonical_ids that share an edge with
    `canonical_id`, bidirectionally. Tenant filter applies to BOTH
    endpoints when set."""
    if tenant_id is None:
        rows = conn.execute(
            """
            SELECT target_node FROM entity_edges WHERE source_node = ?
            UNION
            SELECT source_node FROM entity_edges WHERE target_node = ?
            """,
            (canonical_id, canonical_id),
        ).fetchall()
        return {nid for (nid,) in rows}

    rows = conn.execute(
        """
        SELECT e.target_node
          FROM entity_edges AS e
          JOIN canonical_entities AS cs ON cs.canonical_id = e.source_node
          JOIN canonical_entities AS ct ON ct.canonical_id = e.target_node
         WHERE e.source_node = ?
           AND cs.tenant_id = ?
           AND ct.tenant_id = ?
        UNION
        SELECT e.source_node
          FROM entity_edges AS e
          JOIN canonical_entities AS cs ON cs.canonical_id = e.source_node
          JOIN canonical_entities AS ct ON ct.canonical_id = e.target_node
         WHERE e.target_node = ?
           AND cs.tenant_id = ?
           AND ct.tenant_id = ?
        """,
        (canonical_id, tenant_id, tenant_id, canonical_id, tenant_id, tenant_id),
    ).fetchall()
    return {nid for (nid,) in rows}


def get_created_at(
    conn: sqlite3.Connection,
    canonical_id: str,
    tenant_id: Optional[str] = None,
) -> Optional[datetime]:
    """Return the created_at timestamp for `canonical_id`, or None if absent."""
    if tenant_id is None:
        row = conn.execute(
            "SELECT created_at FROM canonical_entities WHERE canonical_id = ?",
            (canonical_id,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT created_at FROM canonical_entities
             WHERE canonical_id = ?
               AND tenant_id = ?
            """,
            (canonical_id, tenant_id),
        ).fetchone()
    if row is None or row[0] is None:
        return None
    raw = row[0]
    if isinstance(raw, datetime):
        return raw
    # fromisoformat covers every SQLite/ISO shape in one call, but on
    # Python <3.11 it rejects the 'Z' UTC suffix (the format used in the
    # canonical schema examples) — normalize to an offset first, then
    # strip tzinfo so callers compare naive against naive.
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(str(raw), fmt)
        except ValueError:
            pass
    return None


def get_external_field(
    conn: sqlite3.Connection,
    canonical_id: str,
    field_name: str,
    tenant_id: Optional[str] = None,
) -> Optional[str]:
    """Return the first non-None value for `field_name` from any
    system_references.external_fields JSON row for `canonical_id`.

    Uses a LIKE perf hint on the JSON text; authoritative match is done
    in Python, matching the pattern of `lookup_email`.
    """
    needle = f'%"{field_name}"%'
    if tenant_id is None:
        rows = conn.execute(
            """
            SELECT s.external_fields
              FROM system_references AS s
             WHERE s.canonical_id = ?
               AND s.external_fields LIKE ?
            """,
            (canonical_id, needle),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT s.external_fields
              FROM system_references AS s
              JOIN canonical_entities AS c ON c.canonical_id = s.canonical_id
             WHERE s.canonical_id = ?
               AND c.tenant_id = ?
               AND s.external_fields LIKE ?
            """,
            (canonical_id, tenant_id, needle),
        ).fetchall()
    for (fields_json,) in rows:
        if not fields_json:
            continue
        try:
            payload = json.loads(fields_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        val = payload.get(field_name)
        if val is not None:
            return str(val)
    return None


def count_shared_person_neighbors(
    conn: sqlite3.Connection,
    source_canonical_id: Optional[str],
    candidate_canonical_id: str,
    tenant_id: Optional[str] = None,
) -> int:
    """Count canonical_ids of `entity_category='person'` that are
    neighbors of BOTH endpoints via `entity_edges`. Returns 0 when
    `source_canonical_id` is None (Stage 3 query side is typically
    unresolved at scoring time)."""
    if source_canonical_id is None:
        return 0

    src = _neighbors(conn, source_canonical_id, tenant_id)
    cand = _neighbors(conn, candidate_canonical_id, tenant_id)
    shared = src & cand
    if not shared:
        return 0

    placeholders = ",".join("?" for _ in shared)
    if tenant_id is None:
        rows = conn.execute(
            f"""
            SELECT COUNT(*) FROM canonical_entities
             WHERE entity_category = 'person'
               AND canonical_id IN ({placeholders})
            """,
            tuple(shared),
        ).fetchone()
    else:
        rows = conn.execute(
            f"""
            SELECT COUNT(*) FROM canonical_entities
             WHERE entity_category = 'person'
               AND tenant_id = ?
               AND canonical_id IN ({placeholders})
            """,
            (tenant_id, *tuple(shared)),
        ).fetchone()
    return int(rows[0]) if rows else 0


def count_shared_graph_neighbors(
    conn: sqlite3.Connection,
    source_canonical_id: Optional[str],
    candidate_canonical_id: str,
    tenant_id: Optional[str] = None,
) -> int:
    """Count distinct canonical_ids connected to BOTH endpoints via
    `entity_edges`, bidirectionally. Returns 0 when
    `source_canonical_id` is None."""
    if source_canonical_id is None:
        return 0
    src = _neighbors(conn, source_canonical_id, tenant_id)
    cand = _neighbors(conn, candidate_canonical_id, tenant_id)
    return len(src & cand)


# ---------------------------------------------------------------------------
# Stage 3 reads (Signal B3 — amount co-occurrence)
# ---------------------------------------------------------------------------

# AMOUNT_TOLERANCE (rules §5): a co-occurrence exists when two same-
# currency, different-source transaction amounts differ by no more than
# the lesser of a percentage band and a flat dollar cap. USD, V1.
AMOUNT_TOLERANCE_PCT: float = 0.02
AMOUNT_TOLERANCE_CAP: float = 500.0


def count_amount_cooccurrence_periods(
    conn: sqlite3.Connection,
    source: str,
    source_entity_id: str,
    candidate_canonical_id: str,
    tenant_id: Optional[str] = None,
) -> int:
    """Count DISTINCT `period`s (Signal B3) in which a `source`-side
    transaction for `source_entity_id` co-occurs with a same-period,
    same-currency, different-source transaction whose `canonical_id`
    is `candidate_canonical_id`, within `AMOUNT_TOLERANCE`.

    Source side is keyed on `(source, counterparty_source_id)` —
    never on a canonical id, since the entity is typically unresolved
    at Stage 3. Candidate side is keyed on the nullable `canonical_id`
    column. The tolerance predicate anchors on `MAX(|a|, |b|)`, so
    `count_amount_cooccurrence_periods(a, b) ==
    count_amount_cooccurrence_periods(b, a)` by construction. One
    query, no per-row Python filtering.
    """
    if tenant_id is None:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT a.period)
              FROM transactions AS a
              JOIN transactions AS b
                ON b.period = a.period
               AND b.currency = a.currency
               AND b.source != a.source
             WHERE a.source = ?
               AND a.counterparty_source_id = ?
               AND b.canonical_id = ?
               AND ABS(ABS(a.amount) - ABS(b.amount))
                   <= MIN(MAX(ABS(a.amount), ABS(b.amount)) * ?, ?)
            """,
            (
                source,
                source_entity_id,
                candidate_canonical_id,
                AMOUNT_TOLERANCE_PCT,
                AMOUNT_TOLERANCE_CAP,
            ),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT a.period)
              FROM transactions AS a
              JOIN transactions AS b
                ON b.period = a.period
               AND b.currency = a.currency
               AND b.source != a.source
               AND b.tenant_id = a.tenant_id
             WHERE a.source = ?
               AND a.counterparty_source_id = ?
               AND b.canonical_id = ?
               AND a.tenant_id = ?
               AND ABS(ABS(a.amount) - ABS(b.amount))
                   <= MIN(MAX(ABS(a.amount), ABS(b.amount)) * ?, ?)
            """,
            (
                source,
                source_entity_id,
                candidate_canonical_id,
                tenant_id,
                AMOUNT_TOLERANCE_PCT,
                AMOUNT_TOLERANCE_CAP,
            ),
        ).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Feature 14 reads (Overview dashboard / Entity Registry Browser)
#
# List/aggregate/grouped-alias reads the shipped store lacks — Stages 1-4
# above are all per-canonical point lookups. Same conventions: `conn`
# first, `tenant_id: Optional[str] = None` last, no WHERE filter when
# `tenant_id` is None, child tables joined back to `canonical_entities`
# for tenant scoping since they carry no `tenant_id` column of their own.
# ---------------------------------------------------------------------------


def get_canonical_entity(
    conn: sqlite3.Connection,
    canonical_id: str,
    tenant_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Return the full `canonical_entities` row for `canonical_id` as a
    dict, or None if absent (or out of tenant scope)."""
    if tenant_id is None:
        row = conn.execute(
            """
            SELECT canonical_id, canonical_name, entity_type, entity_category,
                   confidence, created_at, updated_at
              FROM canonical_entities
             WHERE canonical_id = ?
            """,
            (canonical_id,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT canonical_id, canonical_name, entity_type, entity_category,
                   confidence, created_at, updated_at
              FROM canonical_entities
             WHERE canonical_id = ?
               AND tenant_id = ?
            """,
            (canonical_id, tenant_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "canonical_id": row[0],
        "canonical_name": row[1],
        "entity_type": row[2],
        "entity_category": row[3],
        "confidence": row[4],
        "created_at": row[5],
        "updated_at": row[6],
    }


def list_canonical_entities(
    conn: sqlite3.Connection,
    tenant_id: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_category: Optional[str] = None,
    min_confidence: Optional[float] = None,
    max_confidence: Optional[float] = None,
    source_category: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return canonical entity rows for the entity browser table:
    `canonical_id`, `canonical_name`, `entity_type`, `entity_category`,
    `confidence`, `alias_count`, `source_categories` (the sorted list of
    distinct `entity_aliases.category` values for that canonical).

    `entity_category` filters on `canonical_entities.entity_category`
    ('organization' | 'person'); `source_category` filters on the
    source-system category carried by `entity_aliases.category` — the
    two are never conflated (rules note in the feature brief). No
    `LIMIT` clause is emitted when `limit` is None, so callers that need
    the full filtered set (e.g. to intersect with a fuzzy-search result)
    can fetch it in one call. Ordered by `canonical_id` for determinism.
    """
    where = ["1 = 1"]
    params: list[Any] = []

    if tenant_id is not None:
        where.append("c.tenant_id = ?")
        params.append(tenant_id)
    if entity_type is not None:
        where.append("c.entity_type = ?")
        params.append(entity_type)
    if entity_category is not None:
        where.append("c.entity_category = ?")
        params.append(entity_category)
    if min_confidence is not None:
        where.append("c.confidence >= ?")
        params.append(min_confidence)
    if max_confidence is not None:
        where.append("c.confidence <= ?")
        params.append(max_confidence)
    if source_category is not None:
        where.append(
            """
            EXISTS (
                SELECT 1 FROM entity_aliases AS sc
                 WHERE sc.canonical_id = c.canonical_id
                   AND sc.category = ?
            )
            """
        )
        params.append(source_category)

    sql = f"""
        SELECT
            c.canonical_id,
            c.canonical_name,
            c.entity_type,
            c.entity_category,
            c.confidence,
            (SELECT COUNT(*) FROM entity_aliases AS a
              WHERE a.canonical_id = c.canonical_id) AS alias_count,
            (SELECT GROUP_CONCAT(DISTINCT a2.category) FROM entity_aliases AS a2
              WHERE a2.canonical_id = c.canonical_id) AS source_categories
          FROM canonical_entities AS c
         WHERE {' AND '.join(where)}
         ORDER BY c.canonical_id
    """
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

    rows = conn.execute(sql, params).fetchall()
    results: list[dict[str, Any]] = []
    for cid, name, etype, ecat, confidence, alias_count, cats in rows:
        source_categories = sorted(cats.split(",")) if cats else []
        results.append(
            {
                "canonical_id": cid,
                "canonical_name": name,
                "entity_type": etype,
                "entity_category": ecat,
                "confidence": confidence,
                "alias_count": int(alias_count),
                "source_categories": source_categories,
            }
        )
    return results


def count_canonical_entities(
    conn: sqlite3.Connection,
    tenant_id: Optional[str] = None,
    min_confidence: Optional[float] = None,
    created_after: Optional[str] = None,
) -> int:
    """Return `COUNT(canonical_entities)` in tenant scope, optionally
    filtered to `confidence >= min_confidence` and/or
    `created_at >= created_after` (an ISO-8601 string, compared as text —
    matching this store's existing `created_at` storage convention).
    """
    where = ["1 = 1"]
    params: list[Any] = []
    if tenant_id is not None:
        where.append("tenant_id = ?")
        params.append(tenant_id)
    if min_confidence is not None:
        where.append("confidence >= ?")
        params.append(min_confidence)
    if created_after is not None:
        where.append("created_at >= ?")
        params.append(created_after)

    row = conn.execute(
        f"SELECT COUNT(*) FROM canonical_entities WHERE {' AND '.join(where)}",
        params,
    ).fetchone()
    return int(row[0]) if row else 0


def count_cross_category_entities(
    conn: sqlite3.Connection,
    tenant_id: Optional[str] = None,
) -> int:
    """Count canonical entities whose aliases span >= 2 distinct
    `entity_aliases.category` values (Cross-Category Coverage
    numerator)."""
    if tenant_id is None:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT canonical_id
                  FROM entity_aliases
                 GROUP BY canonical_id
                HAVING COUNT(DISTINCT category) >= 2
            )
            """
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT a.canonical_id
                  FROM entity_aliases AS a
                  JOIN canonical_entities AS c ON c.canonical_id = a.canonical_id
                 WHERE c.tenant_id = ?
                 GROUP BY a.canonical_id
                HAVING COUNT(DISTINCT a.category) >= 2
            )
            """,
            (tenant_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def get_aliases_grouped_by_category(
    conn: sqlite3.Connection,
    canonical_id: str,
    tenant_id: Optional[str] = None,
) -> dict[str, list[str]]:
    """Return `entity_aliases.value`s for `canonical_id`, grouped by
    `entity_aliases.category` (the source-system category). Excludes no
    rows — unlike `get_aliases`, the canonical-name seed alias is not
    special-cased here, since this reads raw stored rows for display."""
    if tenant_id is None:
        rows = conn.execute(
            """
            SELECT category, value FROM entity_aliases
             WHERE canonical_id = ?
             ORDER BY category, value
            """,
            (canonical_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT a.category, a.value
              FROM entity_aliases AS a
              JOIN canonical_entities AS c ON c.canonical_id = a.canonical_id
             WHERE a.canonical_id = ?
               AND c.tenant_id = ?
             ORDER BY a.category, a.value
            """,
            (canonical_id, tenant_id),
        ).fetchall()
    grouped: dict[str, list[str]] = {}
    for category, value in rows:
        grouped.setdefault(category, []).append(value)
    return grouped


def get_system_references(
    conn: sqlite3.Connection,
    canonical_id: str,
    tenant_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return every `system_references` row for `canonical_id` as a dict
    (source, category, external_id, external_fields — parsed from JSON
    when present). Tenant-scoped when `tenant_id` is set; unlike
    `get_system_refs` (Stage 2d, no tenant filter), this is a display
    read for the entity detail endpoint."""
    if tenant_id is None:
        rows = conn.execute(
            """
            SELECT source, category, external_id, external_fields
              FROM system_references
             WHERE canonical_id = ?
             ORDER BY source, external_id
            """,
            (canonical_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT s.source, s.category, s.external_id, s.external_fields
              FROM system_references AS s
              JOIN canonical_entities AS c ON c.canonical_id = s.canonical_id
             WHERE s.canonical_id = ?
               AND c.tenant_id = ?
             ORDER BY s.source, s.external_id
            """,
            (canonical_id, tenant_id),
        ).fetchall()
    results: list[dict[str, Any]] = []
    for source, category, external_id, fields_json in rows:
        fields: Optional[dict[str, Any]] = None
        if fields_json:
            try:
                fields = json.loads(fields_json)
            except (TypeError, ValueError):
                fields = None
        results.append(
            {
                "source": source,
                "category": category,
                "external_id": external_id,
                "external_fields": fields,
            }
        )
    return results


def get_edges_for_canonical(
    conn: sqlite3.Connection,
    canonical_id: str,
    tenant_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return every `entity_edges` row touching `canonical_id`, in either
    direction, as a dict. Tenant scoping requires BOTH endpoints in
    scope, matching `_neighbors`'s convention above."""
    if tenant_id is None:
        rows = conn.execute(
            """
            SELECT edge_id, source_node, target_node, relationship,
                   source_category, target_category, weight, approval_count
              FROM entity_edges
             WHERE source_node = ? OR target_node = ?
             ORDER BY edge_id
            """,
            (canonical_id, canonical_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT e.edge_id, e.source_node, e.target_node, e.relationship,
                   e.source_category, e.target_category, e.weight, e.approval_count
              FROM entity_edges AS e
              JOIN canonical_entities AS cs ON cs.canonical_id = e.source_node
              JOIN canonical_entities AS ct ON ct.canonical_id = e.target_node
             WHERE (e.source_node = ? OR e.target_node = ?)
               AND cs.tenant_id = ?
               AND ct.tenant_id = ?
             ORDER BY e.edge_id
            """,
            (canonical_id, canonical_id, tenant_id, tenant_id),
        ).fetchall()
    return [
        {
            "edge_id": edge_id,
            "source_node": source_node,
            "target_node": target_node,
            "relationship": relationship,
            "source_category": source_category,
            "target_category": target_category,
            "weight": weight,
            "approval_count": approval_count,
        }
        for edge_id, source_node, target_node, relationship, source_category, target_category, weight, approval_count in rows
    ]


def list_entities_for_search(
    conn: sqlite3.Connection,
    tenant_id: Optional[str] = None,
) -> list[tuple[str, str, list[str]]]:
    """Return `(canonical_id, canonical_name, alias_values)` for every
    tenant-scoped canonical — raw rows only, for a caller outside this
    module to fuzzy-score (this file must not import RapidFuzz; see
    `tests/test_blocking.py::test_no_rapidfuzz_in_matching_modules`)."""
    if tenant_id is None:
        canon_rows = conn.execute(
            "SELECT canonical_id, canonical_name FROM canonical_entities"
        ).fetchall()
    else:
        canon_rows = conn.execute(
            "SELECT canonical_id, canonical_name FROM canonical_entities WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchall()

    aliases_by_canonical: dict[str, list[str]] = {}
    for canonical_id, _name in canon_rows:
        aliases_by_canonical[canonical_id] = get_aliases(conn, canonical_id, tenant_id)

    return [
        (canonical_id, name, aliases_by_canonical.get(canonical_id, []))
        for canonical_id, name in canon_rows
    ]


# ---------------------------------------------------------------------------
# Stage 4 reads (threshold / cluster conflict)
# ---------------------------------------------------------------------------


def are_clustered(
    conn: sqlite3.Connection,
    cid_a: str,
    cid_b: str,
    tenant_id: Optional[str] = None,
) -> bool:
    """Return True iff a SAME_AS edge exists between `cid_a` and `cid_b`
    (in either direction) in `entity_edges`.

    V1 has no Stage 6 (resolution / graph update) writes yet, so this
    returns False for every production call — shipped now so Stage 6
    can populate `entity_edges` rows with `relationship='SAME_AS'`
    without revisiting Stage 4.

    Tenant scoping: when `tenant_id` is set, BOTH endpoints must belong
    to the tenant for the edge to count. A cross-tenant edge (which
    shouldn't exist in V1 anyway) is ignored under a scoped query.
    """
    if cid_a == cid_b:
        return False  # same canonical — not a conflict and not a cluster pair

    if tenant_id is None:
        row = conn.execute(
            """
            SELECT 1 FROM entity_edges
             WHERE relationship = 'SAME_AS'
               AND ((source_node = ? AND target_node = ?)
                 OR (source_node = ? AND target_node = ?))
             LIMIT 1
            """,
            (cid_a, cid_b, cid_b, cid_a),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT 1
              FROM entity_edges AS e
              JOIN canonical_entities AS cs ON cs.canonical_id = e.source_node
              JOIN canonical_entities AS ct ON ct.canonical_id = e.target_node
             WHERE e.relationship = 'SAME_AS'
               AND cs.tenant_id = ?
               AND ct.tenant_id = ?
               AND ((e.source_node = ? AND e.target_node = ?)
                 OR (e.source_node = ? AND e.target_node = ?))
             LIMIT 1
            """,
            (tenant_id, tenant_id, cid_a, cid_b, cid_b, cid_a),
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Stage 6 writes (resolution / graph update)
# ---------------------------------------------------------------------------
#
# None of the functions below call `conn.commit()` or `conn.rollback()` —
# the transaction boundary belongs exclusively to `core.graph.resolution`.
# `tenant_id` is a real column on `canonical_entities` only; on
# `entity_aliases` / `entity_edges` / `system_references` (which carry no
# `tenant_id` column) it is a scoping filter applied by verifying the
# parent `canonical_entities` row is in tenant scope before writing —
# never an inserted value.


def _assert_tenant_scope(
    conn: sqlite3.Connection,
    canonical_id: str,
    tenant_id: Optional[str],
) -> None:
    """Raise ValueError if `canonical_id` is not visible under `tenant_id`.

    No-op when `tenant_id` is None (V1 single-tenant default).
    """
    if tenant_id is None:
        return
    row = conn.execute(
        "SELECT 1 FROM canonical_entities WHERE canonical_id = ? AND tenant_id = ?",
        (canonical_id, tenant_id),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"canonical_id {canonical_id!r} not visible under tenant_id {tenant_id!r}"
        )


def create_canonical_entity(
    conn: sqlite3.Connection,
    canonical_name: str,
    entity_type: str,
    entity_category: str,
    confidence: float,
    tenant_id: Optional[str] = None,
) -> str:
    """Insert a new `canonical_entities` row and return its generated id.

    `canonical_id` is generated as `f"{entity_type.upper()}_{hex8}"`
    (rules §3 shape, e.g. `CLIENT_0042`), never caller-supplied — Stage 6
    is the only writer that mints canonical ids. `tenant_id` is written
    as a real column value (nullable).
    """
    canonical_id = f"{entity_type.upper()}_{uuid.uuid4().hex[:8].upper()}"
    conn.execute(
        """
        INSERT INTO canonical_entities (
            canonical_id, tenant_id, canonical_name, entity_type, entity_category, confidence
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (canonical_id, tenant_id, canonical_name, entity_type, entity_category, confidence),
    )
    return canonical_id


def add_alias(
    conn: sqlite3.Connection,
    canonical_id: str,
    value: str,
    source: str,
    category: str,
    confidence: float,
    tenant_id: Optional[str] = None,
) -> int:
    """Idempotently add an `entity_aliases` row and return its `alias_id`.

    Relies on the shipped `UNIQUE (canonical_id, value, source)`
    constraint: `INSERT ... ON CONFLICT DO NOTHING`, then a SELECT to
    return the (new or pre-existing) `alias_id`. `tenant_id` is a
    scoping filter only — `entity_aliases` has no `tenant_id` column.
    """
    _assert_tenant_scope(conn, canonical_id, tenant_id)
    conn.execute(
        """
        INSERT INTO entity_aliases (canonical_id, value, source, category, confidence)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (canonical_id, value, source) DO NOTHING
        """,
        (canonical_id, value, source, category, confidence),
    )
    row = conn.execute(
        """
        SELECT alias_id FROM entity_aliases
         WHERE canonical_id = ? AND value = ? AND source = ?
        """,
        (canonical_id, value, source),
    ).fetchone()
    return int(row[0])


def add_system_reference(
    conn: sqlite3.Connection,
    canonical_id: str,
    source: str,
    category: str,
    external_id: str,
    external_fields: Optional[dict[str, Any]],
    tenant_id: Optional[str] = None,
) -> int:
    """Idempotently upsert a `system_references` row on `(source,
    external_id)` and return its `ref_id`.

    `UNIQUE (source, external_id)` is GLOBAL, not per-canonical. When an
    existing row for that key already belongs to `canonical_id`, it is
    updated in place (idempotent re-write). When it belongs to a
    DIFFERENT canonical, the write is refused — an unguarded upsert
    would silently re-point a source-system reference across
    canonicals. `tenant_id` is a scoping filter only — `system_references`
    has no `tenant_id` column.
    """
    _assert_tenant_scope(conn, canonical_id, tenant_id)
    fields_json = json.dumps(external_fields, sort_keys=True) if external_fields else None

    existing = conn.execute(
        "SELECT ref_id, canonical_id FROM system_references WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()
    if existing is not None:
        existing_ref_id, existing_canonical_id = existing
        if existing_canonical_id != canonical_id:
            raise ValueError(
                f"system_reference (source={source!r}, external_id={external_id!r}) "
                f"already bound to canonical_id {existing_canonical_id!r}; "
                f"refusing to re-point it to {canonical_id!r}"
            )
        conn.execute(
            "UPDATE system_references SET category = ?, external_fields = ? WHERE ref_id = ?",
            (category, fields_json, existing_ref_id),
        )
        return int(existing_ref_id)

    cur = conn.execute(
        """
        INSERT INTO system_references (canonical_id, source, category, external_id, external_fields)
        VALUES (?, ?, ?, ?, ?)
        """,
        (canonical_id, source, category, external_id, fields_json),
    )
    return int(cur.lastrowid)


def upsert_edge(
    conn: sqlite3.Connection,
    source_node: str,
    target_node: str,
    relationship: str,
    source_category: str,
    target_category: str,
    weight: float,
    approved_by: str,
    tenant_id: Optional[str] = None,
) -> int:
    """Idempotently create-or-reconfirm an `entity_edges` row.

    `entity_edges` carries NO uniqueness constraint, so idempotency is a
    hand-written SELECT-then-branch on `(source_node, target_node,
    relationship)`: when a row already exists, delegate to
    `increment_approval_count` instead of inserting a second edge (no
    duplicate-edge row is ever created). Otherwise insert a fresh row
    with `approval_count = 1`. Returns the `edge_id` in either case.
    `tenant_id` scopes BOTH endpoints — `entity_edges` has no
    `tenant_id` column.
    """
    _assert_tenant_scope(conn, source_node, tenant_id)
    _assert_tenant_scope(conn, target_node, tenant_id)

    existing = conn.execute(
        """
        SELECT edge_id FROM entity_edges
         WHERE source_node = ? AND target_node = ? AND relationship = ?
        """,
        (source_node, target_node, relationship),
    ).fetchone()
    if existing is not None:
        edge_id = int(existing[0])
        increment_approval_count(conn, edge_id)
        return edge_id

    cur = conn.execute(
        """
        INSERT INTO entity_edges (
            source_node, target_node, relationship,
            source_category, target_category, weight, approved_by, approval_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (source_node, target_node, relationship, source_category, target_category, weight, approved_by),
    )
    return int(cur.lastrowid)


def increment_approval_count(conn: sqlite3.Connection, edge_id: int) -> int:
    """Increment `entity_edges.approval_count` by 1 and return the new value."""
    conn.execute(
        "UPDATE entity_edges SET approval_count = approval_count + 1 WHERE edge_id = ?",
        (edge_id,),
    )
    row = conn.execute(
        "SELECT approval_count FROM entity_edges WHERE edge_id = ?",
        (edge_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def update_confidence(
    conn: sqlite3.Connection,
    canonical_id: str,
    new_confidence: float,
    tenant_id: Optional[str] = None,
) -> None:
    """Update `canonical_entities.confidence` (and `updated_at`) for
    `canonical_id`. When `tenant_id` is set, scopes the WHERE clause to
    that tenant — a mismatched tenant leaves the row untouched.
    """
    if tenant_id is None:
        conn.execute(
            """
            UPDATE canonical_entities
               SET confidence = ?, updated_at = CURRENT_TIMESTAMP
             WHERE canonical_id = ?
            """,
            (new_confidence, canonical_id),
        )
    else:
        conn.execute(
            """
            UPDATE canonical_entities
               SET confidence = ?, updated_at = CURRENT_TIMESTAMP
             WHERE canonical_id = ? AND tenant_id = ?
            """,
            (new_confidence, canonical_id, tenant_id),
        )
