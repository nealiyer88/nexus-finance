"""Append-only resolution audit trail (feature 10c).

`log_resolution` writes exactly one `audit_log` row per call and issues no
other statement against that table — no code path in this module ever
reads back, modifies, or removes a row it wrote.

`actor_id` is inserted as SQL NULL on every call, mandatory rather than
optional: the schema types it `UUID` and a human-readable actor string
(system identity or user id) is not a UUID, so it is recorded instead
inside the `diff` JSONB payload under a stable key. No code path here
binds the caller's actor value to `actor_id`.

Connections arrive already open via `core.graph.pg.connect()` — this
module never imports the driver to open one, never reads the environment,
and never builds a DSN from parts. `DATABASE_URL` never reaches a log
line, a print, or an exception message raised from here.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Optional

from core.graph.tenants import resolve_tenant_for_write

# Stable key under which the human-readable actor identity lives inside
# `audit_log.diff`, since `actor_id` itself is always NULL (see above).
ACTOR_DIFF_KEY = "actor"


def _to_jsonable(value: Any) -> Any:
    """Best-effort conversion of a dataclass to a JSON-serializable shape;
    plain dicts/strings/None pass through unchanged."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return value


def log_resolution(
    conn,
    canonical_id: str,
    incoming_entity_raw: str,
    match_type: str,
    confidence: float,
    signals: Any,
    category_pair: str,
    user_id: Optional[str],
    tenant_id: Optional[str],
) -> None:
    """Insert one append-only `audit_log` row for a Stage 6 resolution.

    `action` carries `match_type`, `resource`/`resource_id` name the
    mutated canonical entity, `category` carries `category_pair` verbatim,
    and `diff` is a JSONB payload holding `confidence`, `signals`, the raw
    incoming entity string, and the human-readable actor under
    `ACTOR_DIFF_KEY`. `actor_id` is always NULL.
    """
    resolved_tenant_id = resolve_tenant_for_write(conn, tenant_id)

    diff = {
        "match_type": match_type,
        "confidence": confidence,
        "signals": _to_jsonable(signals),
        "incoming_entity_raw": incoming_entity_raw,
        ACTOR_DIFF_KEY: user_id,
    }

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_log (
                tenant_id, actor_id, action, resource, resource_id, category, diff
            ) VALUES (%s, NULL, %s, %s, %s, %s, %s)
            """,
            (
                resolved_tenant_id,
                match_type,
                "canonical_entities",
                canonical_id,
                category_pair,
                json.dumps(diff),
            ),
        )
