"""Overview dashboard — system-health KPI cards (feature 14).

Renders one card per `KPI_MAPPING` entry: Entities Resolved, Auto-Match
Rate, Pending Approvals, Cross-Category Coverage (`.claude/rules/
01-nexus-finance-v1.md` §... / feature brief KPI Definitions table).
`KPI_MAPPING` is the single source of truth for the card set — the
layout builds exactly one card per key, so the card set and the metric
set cannot drift.

The layout below builds with no database present: no connection, no
query, no store file touched at import or layout-build time (matching
`dashboard/pages/approval_queue.py`'s convention). All data access
lives inside the `@callback` body below, which calls `api.routers.
entities.compute_stats` directly — the same function `GET
/entities/stats` wraps — obtaining its SQLite connection exclusively
through `core.matching.pending_store.get_connection`.
"""

from __future__ import annotations

import os
from typing import Any, Dict

import dash
from dash import Input, Output, callback, html

from api.middleware.tenant import DEFAULT_TENANT_ID
from api.routers.entities import compute_stats
from core.matching.pending_store import get_connection

dash.register_page(__name__, path="/", name="Overview")

# Card id -> display label. The layout emits exactly one card per key;
# the callback below emits exactly one value per key. Keep these keys
# identical to `compute_stats`'s return keys.
KPI_MAPPING: Dict[str, str] = {
    "resolved_entities": "Entities Resolved",
    "auto_match_rate": "Auto-Match Rate",
    "pending_approvals": "Pending Approvals",
    "cross_category_coverage": "Cross-Category Coverage",
}


def _resolve_tenant_id() -> str:
    return os.environ.get("NEXUS_TENANT_ID", DEFAULT_TENANT_ID)


def _format_kpi_value(key: str, value: Any) -> str:
    """Pure formatting — no I/O. Rate/coverage fields render as a
    percentage; counts render as-is."""
    if value is None:
        return "—"
    if key in ("auto_match_rate", "cross_category_coverage"):
        return f"{value * 100:.1f}%"
    return str(value)


def _kpi_card(key: str, label: str) -> html.Div:
    return html.Div(
        [
            html.Div(label, id=f"{key}-label"),
            html.Div("—", id=f"{key}-value"),
        ],
        id=key,
    )


def layout() -> html.Div:
    return html.Div(
        [
            html.H1("Overview"),
            html.Div(
                [_kpi_card(key, label) for key, label in KPI_MAPPING.items()],
                id="overview-kpi-cards",
            ),
        ]
    )


@callback(
    [Output(f"{key}-value", "children") for key in KPI_MAPPING],
    Input("_pages_location", "pathname"),
)
def refresh_kpi_cards(_pathname):
    tenant_id = _resolve_tenant_id()
    with get_connection() as conn:
        stats = compute_stats(conn, tenant_id)
    return [_format_kpi_value(key, stats.get(key)) for key in KPI_MAPPING]
