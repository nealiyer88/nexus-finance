"""AR reconciliation page (feature 15): RUDDR labor vs. QB invoiced
revenue, per resolved canonical client.

The layout below builds with no database present: no connection, no
query, no store file required at import or layout-build time. All data
access lives inside `@callback` functions, which obtain their SQLite
connection exclusively through `core.matching.pending_store.
get_connection` and their aggregation from `core.reconciliation.ar` —
the same functions `api/routers/reconciliation.py` calls, so the API
and this page can never report two different numbers.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import dash
from dash import Input, Output, State, callback, dash_table, dcc, html

from api.middleware.tenant import DEFAULT_TENANT_ID
from core.graph.entity_store import AMOUNT_TOLERANCE_CAP, AMOUNT_TOLERANCE_PCT
from core.matching.pending_store import get_connection
from core.reconciliation.ar import get_ar_detail, get_ar_report

dash.register_page(__name__, path="/ar-reconciliation", name="AR Reconciliation")

TABLE_COLUMNS: List[Dict[str, str]] = [
    {"name": "Client", "id": "canonical_name"},
    {"name": "RUDDR Labor $", "id": "labor_total"},
    {"name": "QB Invoiced $", "id": "invoiced_total"},
    {"name": "Variance $", "id": "variance"},
    {"name": "Variance %", "id": "variance_pct"},
    {"name": "Status", "id": "status"},
]

# Status colors: green (matched within tolerance), amber (unbilled
# beyond tolerance), red (variance beyond the variance-% band).
_STATUS_COLORS: Dict[str, str] = {
    "MATCHED": "#d4edda",
    "UNBILLED": "#fff3cd",
    "OVERBILLED": "#f8d7da",
}


# ---------------------------------------------------------------------------
# Tenant resolution — this is a background/worker-side module, not an
# HTTP request handler, so there is no `request.state.tenant_id` to
# read here. Mirrors the same precedence `dashboard/pages/
# approval_queue.py` already uses.
# ---------------------------------------------------------------------------


def _resolve_tenant_id() -> str:
    return os.environ.get("NEXUS_TENANT_ID", DEFAULT_TENANT_ID)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def classify_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Status-assignment helper. Pure — no I/O. `rows` are report rows
    carrying `labor_total` / `invoiced_total`; tolerance is imported —
    never re-literalled — from `core.graph.entity_store`, matching
    rules §5's `min(max(|a|, |b|) * AMOUNT_TOLERANCE_PCT,
    AMOUNT_TOLERANCE_CAP)`."""
    classified: List[Dict[str, Any]] = []
    for row in rows:
        labor_total = float(row.get("labor_total") or 0.0)
        invoiced_total = float(row.get("invoiced_total") or 0.0)
        variance = labor_total - invoiced_total
        tolerance = min(
            max(abs(labor_total), abs(invoiced_total)) * AMOUNT_TOLERANCE_PCT,
            AMOUNT_TOLERANCE_CAP,
        )
        if abs(variance) <= tolerance:
            status = "MATCHED"
        elif variance > tolerance:
            status = "UNBILLED"
        else:
            status = "OVERBILLED"
        denominator = invoiced_total or labor_total
        variance_pct = (variance / denominator * 100.0) if denominator else 0.0
        classified.append(
            {
                **row,
                "variance": variance,
                "variance_pct": variance_pct,
                "status": status,
            }
        )
    return classified


# ---------------------------------------------------------------------------
# Layout — pure at build time, no database access.
# ---------------------------------------------------------------------------


def layout() -> html.Div:
    return html.Div(
        [
            html.H1("AR Reconciliation"),
            dash_table.DataTable(
                id="ar-recon-table",
                columns=TABLE_COLUMNS,
                data=[],
                page_action="native",
                page_size=25,
                sort_action="native",
                style_data_conditional=[
                    {
                        "if": {"filter_query": '{{status}} = "{}"'.format(status)},
                        "backgroundColor": color,
                    }
                    for status, color in _STATUS_COLORS.items()
                ],
            ),
            html.Div(id="ar-recon-detail"),
        ]
    )


# ---------------------------------------------------------------------------
# Callbacks — all database access lives here.
# ---------------------------------------------------------------------------


@callback(
    Output("ar-recon-table", "data"),
    Input("_pages_location", "pathname"),
)
def refresh_ar_table(_pathname: Optional[str]) -> List[Dict[str, Any]]:
    tenant_id = _resolve_tenant_id()
    with get_connection() as conn:
        rows = get_ar_report(conn, tenant_id)
    return classify_rows(rows)


@callback(
    Output("ar-recon-detail", "children"),
    Input("ar-recon-table", "active_cell"),
    State("ar-recon-table", "data"),
)
def show_ar_detail(
    active_cell: Optional[dict], table_data: Optional[List[Dict[str, Any]]]
):
    if not active_cell or not table_data:
        return html.Div("Select a row to view details.")
    row = table_data[active_cell["row"]]
    canonical_id = row.get("canonical_id")
    tenant_id = _resolve_tenant_id()
    with get_connection() as conn:
        detail = get_ar_detail(conn, canonical_id, tenant_id)
    if detail is None:
        return html.Div("Detail not found.")

    def _line(txn: Dict[str, Any]) -> html.Li:
        return html.Li(
            f"{txn['txn_date']} · {txn['source']} · {txn['txn_type']} · {txn['amount']} {txn['currency']}"
        )

    return html.Div(
        [
            html.Div(
                [
                    html.H4("RUDDR (PSA)"),
                    html.Ul(
                        [_line(t) for t in detail["psa_transactions"]],
                        id="ar-recon-detail-psa-list",
                    ),
                ],
                id="ar-recon-detail-psa",
            ),
            html.Div(
                [
                    html.H4("QuickBooks (Accounting)"),
                    html.Ul(
                        [_line(t) for t in detail["accounting_transactions"]],
                        id="ar-recon-detail-accounting-list",
                    ),
                ],
                id="ar-recon-detail-accounting",
            ),
        ]
    )
