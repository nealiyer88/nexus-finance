"""Approval-decision capture for the Postgres operational store (feature 10c).

`record_approval_decision` writes one `approval_decisions` row per call.
Its `disposition` value is translated through 10a's `core.graph.dispositions
.MAPPING` — this module adds no vocabulary of its own and no CHECK value,
terminal value, or set literal is written here or in this feature's tests
for it; both sets are derived by parsing at test time.

Connections arrive already open via `core.graph.pg.connect()` — this
module never imports the driver to open one, never reads the environment,
and never builds a DSN from parts.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Optional

from core.graph.dispositions import MAPPING
from core.graph.tenants import resolve_tenant_for_write


def _to_jsonable(value: Any) -> Any:
    """Best-effort conversion of a dataclass to a JSON-serializable shape;
    plain dicts/strings/None pass through unchanged."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return value


def record_approval_decision(
    conn,
    entity_pair_a: str,
    entity_pair_b: str,
    pending_status: str,
    signal_breakdown: Any,
    graph_evidence: Any,
    category_pair: Optional[str],
    reasoning_trace: str,
    confidence_at_decision: Optional[float],
    decided_by: Optional[str],
    tenant_id: Optional[str],
) -> None:
    """Insert one `approval_decisions` row.

    `pending_status` is a terminal `pending_decisions.status` value (a key
    of `MAPPING`); the row's `disposition` column is written as
    `MAPPING[pending_status]`, which satisfies `approval_decisions
    .disposition`'s CHECK for every key `MAPPING` carries. `KeyError` on a
    non-terminal status propagates rather than being swallowed.
    """
    disposition = MAPPING[pending_status]
    resolved_tenant_id = resolve_tenant_for_write(conn, tenant_id)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO approval_decisions (
                tenant_id, entity_pair_a, entity_pair_b, signal_breakdown,
                graph_evidence, category_pair, disposition, reasoning_trace,
                confidence_at_decision, decided_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                resolved_tenant_id,
                entity_pair_a,
                entity_pair_b,
                json.dumps(_to_jsonable(signal_breakdown)),
                json.dumps(_to_jsonable(graph_evidence)),
                category_pair,
                disposition,
                reasoning_trace,
                confidence_at_decision,
                decided_by,
            ),
        )
