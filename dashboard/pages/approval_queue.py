"""Approval queue page — pending Stage 4 decisions (feature 11).

Renders 10b's `pending_decisions` rows for human review: a table of
incoming vs. candidate entities, an expandable detail view (signal
breakdown, graph evidence, LLM reasoning when a Stage 5 assessment is
present), and approve / reject / correct actions per row. The detail
view shows raw, un-normalized names on both sides — no redaction, no
hashing, no normalization-away of 10b's deliberate no-redaction stance
for this table.

The layout below builds with no database present: no connection, no
query, no store file required at import or layout-build time. All data
access — the table's contents, the detail view, the action callbacks,
and the sidebar badge count — lives inside `@callback` functions, which
obtain their SQLite connection exclusively through
`core.matching.pending_store.get_connection`, the same provider this
feature adds to that module. This module never imports the standard
library SQLite driver directly and never opens a raw connection of its
own.

The badge callback attaches to the reserved slot
`{"type": "nav-badge", "path": "/approval-queue"}` that `dashboard/app.py`
renders empty, per that module's docstring — without editing
`dashboard/app.py`.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any, Dict, List, Optional

import dash
from dash import Input, Output, State, callback, dash_table, dcc, html

from api.middleware.tenant import DEFAULT_TENANT_ID
from api.routers.approvals import (
    PendingNotFoundError,
    TerminalRowError,
    approve_decision,
    correct_decision,
    reject_decision,
)
from core.graph.entity_store import get_canonical_name_and_category
from core.matching.pending_store import get_connection, get_pending, list_pending, rehydrate

dash.register_page(__name__, path="/approval-queue", name="Approval Queue")

TABLE_COLUMNS: List[Dict[str, str]] = [
    {"name": "Incoming Entity", "id": "incoming_entity_name"},
    {"name": "Candidate Entity", "id": "candidate_entity_name"},
    {"name": "Source Categories", "id": "source_categories"},
    {"name": "Confidence", "id": "confidence_score"},
    {"name": "Match Type", "id": "match_type"},
]


# ---------------------------------------------------------------------------
# Tenant resolution — this is a background/worker-side module, not an
# HTTP request handler, so there is no `request.state.tenant_id` to
# read here. Mirrors the same `NEXUS_TENANT_ID` env-var / bootstrap
# default precedence `api/main.py`'s own startup hook already uses; it
# resolves no tenant of its own scheme and installs no middleware.
# ---------------------------------------------------------------------------


def _resolve_tenant_id() -> str:
    return os.environ.get("NEXUS_TENANT_ID", DEFAULT_TENANT_ID)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def apply_filters(
    rows: List[Dict[str, Any]],
    entity_category: Optional[str] = None,
    min_confidence: Optional[float] = None,
    max_confidence: Optional[float] = None,
    category_pair: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Filter `rows` by the queue's filter controls. Pure — no I/O.

    `entity_category` is the person/organization axis; `category_pair`
    is the accounting/psa axis (e.g. `"psa:accounting"`) — the two are
    never conflated. Each keyword argument is applied only when not
    `None`.
    """
    filtered = rows
    if entity_category is not None:
        filtered = [r for r in filtered if r.get("entity_category") == entity_category]
    if min_confidence is not None:
        filtered = [
            r
            for r in filtered
            if r.get("confidence_score") is not None and r["confidence_score"] >= min_confidence
        ]
    if max_confidence is not None:
        filtered = [
            r
            for r in filtered
            if r.get("confidence_score") is not None and r["confidence_score"] <= max_confidence
        ]
    if category_pair is not None:
        filtered = [r for r in filtered if r.get("category_pair") == category_pair]
    return filtered


def _candidate_name(conn: Any, canonical_id: Optional[str], tenant_id: Optional[str]) -> Optional[str]:
    if canonical_id is None:
        return None
    row = get_canonical_name_and_category(conn, canonical_id, tenant_id)
    return row[0] if row is not None else canonical_id


def _match_type(disposition: Any) -> str:
    if disposition.llm_assessment is not None:
        return "llm"
    if disposition.abbreviation_rescue:
        return "abbreviation_rescue"
    return "scored"


def row_from_pending(conn: Any, pending: Any, tenant_id: Optional[str]) -> Dict[str, Any]:
    """Build one table row (plus the filter-only fields `entity_category`
    and `category_pair`) from a `PendingDecision`. Pure given `conn`."""
    disposition, entity, _proposal = rehydrate(pending)
    return {
        "pending_id": pending.pending_id,
        "incoming_entity_name": entity.raw_name,
        "candidate_entity_name": _candidate_name(conn, pending.top_canonical_id, tenant_id),
        "source_categories": pending.category_pair,
        "confidence_score": pending.top_score,
        "match_type": _match_type(disposition),
        "entity_category": entity.entity_category,
        "category_pair": pending.category_pair,
    }


def build_detail_view(
    pending: Any,
    disposition: Any,
    entity: Any,
    candidate_name: Optional[str] = None,
) -> html.Div:
    """Build the expandable detail view for one pending item. Pure — no
    I/O. Shows raw, un-normalized names on both sides; signal breakdown
    and graph evidence when a top match exists; LLM reasoning only when
    `disposition.llm_assessment` is present (absent, not empty-stringed,
    otherwise)."""
    top = disposition.top_match
    children: List[Any] = [
        html.H3("Pending item detail", id="detail-heading"),
        html.Div(f"Incoming (raw): {entity.raw_name}", id="detail-incoming-raw-name"),
        html.Div(
            f"Candidate (raw): {candidate_name if candidate_name is not None else pending.top_canonical_id}",
            id="detail-candidate-raw-name",
        ),
    ]
    if top is not None:
        children.append(
            html.Div(
                f"Signal breakdown: {dataclasses.asdict(top.signal_breakdown)}",
                id="detail-signal-breakdown",
            )
        )
        children.append(
            html.Div(
                f"Graph evidence: {dataclasses.asdict(top.graph_evidence)}",
                id="detail-graph-evidence",
            )
        )
    if disposition.llm_assessment is not None:
        children.append(
            html.Div(
                f"LLM reasoning: {disposition.llm_assessment.reasoning}",
                id="detail-llm-reasoning",
            )
        )
    return html.Div(children, id="pending-detail-content")


def pending_count(tenant_id: Optional[str] = None) -> int:
    """Module-level pending-count helper: the count of 10b's `pending`
    rows for `tenant_id` (all tenants when `None`)."""
    with get_connection() as conn:
        if tenant_id is None:
            row = conn.execute(
                "SELECT COUNT(*) FROM pending_decisions WHERE status = 'pending'"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM pending_decisions WHERE status = 'pending' AND tenant_id = ?",
                (tenant_id,),
            ).fetchone()
    return int(row[0]) if row is not None else 0


# ---------------------------------------------------------------------------
# Layout — pure at build time, no database access.
# ---------------------------------------------------------------------------


def layout() -> html.Div:
    return html.Div(
        [
            html.H1("Approval Queue"),
            html.Div(
                [
                    dcc.Dropdown(
                        id="approval-filter-entity-category",
                        options=[
                            {"label": "Organization", "value": "organization"},
                            {"label": "Person", "value": "person"},
                        ],
                        placeholder="entity category",
                    ),
                    dcc.Input(
                        id="approval-filter-confidence-min",
                        type="number",
                        placeholder="min confidence",
                    ),
                    dcc.Input(
                        id="approval-filter-confidence-max",
                        type="number",
                        placeholder="max confidence",
                    ),
                    dcc.Input(
                        id="approval-filter-category-pair",
                        placeholder="category pair (e.g. psa:accounting)",
                    ),
                ]
            ),
            dash_table.DataTable(
                id="pending-table",
                columns=TABLE_COLUMNS,
                data=[],
                page_action="native",
                page_size=25,
                sort_action="native",
            ),
            html.Div(id="pending-detail"),
            html.Div(
                [
                    html.Button("Approve", id="approval-action-approve", n_clicks=0),
                    html.Button("Reject", id="approval-action-reject", n_clicks=0),
                    dcc.Input(
                        id="approval-correct-canonical-id",
                        placeholder="correction canonical id",
                    ),
                    html.Button("Correct", id="approval-action-correct", n_clicks=0),
                    html.Div(id="approval-action-status"),
                ]
            ),
        ]
    )


# ---------------------------------------------------------------------------
# Callbacks — all database access lives here.
# ---------------------------------------------------------------------------


@callback(
    Output("pending-table", "data"),
    Input("_pages_location", "pathname"),
    Input("approval-filter-entity-category", "value"),
    Input("approval-filter-confidence-min", "value"),
    Input("approval-filter-confidence-max", "value"),
    Input("approval-filter-category-pair", "value"),
    Input("approval-action-status", "children"),
)
def refresh_pending_table(
    _pathname: Optional[str],
    entity_category: Optional[str],
    min_confidence: Optional[float],
    max_confidence: Optional[float],
    category_pair: Optional[str],
    _action_status: Any,
) -> List[Dict[str, Any]]:
    tenant_id = _resolve_tenant_id()
    with get_connection() as conn:
        pending_rows = list_pending(conn, tenant_id=tenant_id, limit=1000, offset=0)
        rows = [row_from_pending(conn, p, tenant_id) for p in pending_rows]
    return apply_filters(
        rows,
        entity_category=entity_category or None,
        min_confidence=min_confidence,
        max_confidence=max_confidence,
        category_pair=category_pair or None,
    )


@callback(
    Output("pending-detail", "children"),
    Input("pending-table", "active_cell"),
    State("pending-table", "data"),
)
def show_pending_detail(active_cell: Optional[dict], table_data: Optional[List[Dict[str, Any]]]):
    if not active_cell or not table_data:
        return html.Div("Select a row to view details.")
    row = table_data[active_cell["row"]]
    pending_id = row.get("pending_id")
    tenant_id = _resolve_tenant_id()
    with get_connection() as conn:
        pending = get_pending(conn, pending_id, tenant_id)
        if pending is None:
            return html.Div("Item not found.")
        disposition, entity, _proposal = rehydrate(pending)
        candidate_name = _candidate_name(conn, pending.top_canonical_id, tenant_id)
    return build_detail_view(pending, disposition, entity, candidate_name)


@callback(
    Output("approval-action-status", "children"),
    Input("approval-action-approve", "n_clicks"),
    Input("approval-action-reject", "n_clicks"),
    Input("approval-action-correct", "n_clicks"),
    State("pending-table", "active_cell"),
    State("pending-table", "data"),
    State("approval-correct-canonical-id", "value"),
    prevent_initial_call=True,
)
def handle_action(
    _approve_clicks: int,
    _reject_clicks: int,
    _correct_clicks: int,
    active_cell: Optional[dict],
    table_data: Optional[List[Dict[str, Any]]],
    correction_canonical_id: Optional[str],
):
    if not active_cell or not table_data:
        return "Select a row before choosing an action."

    row = table_data[active_cell["row"]]
    pending_id = row.get("pending_id")
    tenant_id = _resolve_tenant_id()
    triggered = [t["prop_id"].split(".")[0] for t in dash.callback_context.triggered]
    action = triggered[0] if triggered else None

    with get_connection() as conn:
        try:
            if action == "approval-action-approve":
                approve_decision(conn, tenant_id, pending_id)
                return f"Approved {pending_id}."
            if action == "approval-action-reject":
                reject_decision(conn, tenant_id, pending_id)
                return f"Rejected {pending_id}."
            if action == "approval-action-correct":
                if not correction_canonical_id:
                    return "Enter a canonical id to correct to."
                correct_decision(conn, tenant_id, pending_id, correction_canonical_id)
                return f"Corrected {pending_id} to {correction_canonical_id}."
        except PendingNotFoundError:
            return f"{pending_id} not found."
        except TerminalRowError:
            return f"{pending_id} was already decided."
        except ValueError as exc:
            return str(exc)
    return dash.no_update


@callback(
    Output({"type": "nav-badge", "path": "/approval-queue"}, "children"),
    Input("_pages_location", "pathname"),
    Input("approval-action-status", "children"),
)
def update_nav_badge(_pathname: Optional[str], _action_status: Any) -> int:
    return pending_count(_resolve_tenant_id())
