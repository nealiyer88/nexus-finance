"""Audit log page — filterable, append-only, paginated (feature 16).

Displays every row `api.middleware.audit` writes plus 10c's Stage 6
resolution rows (`core.graph.audit.log_resolution`) — both share the same
`audit_log` table; this page does not distinguish them. No edit or delete
UI action exists anywhere on this page.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import dash
from dash import dcc, html
from dash import dash_table

dash.register_page(__name__, path="/audit-log", name="Audit Log")

DISPLAY_COLUMNS = [
    "created_at",
    "actor_id",
    "action",
    "resource",
    "resource_id",
    "category",
    "diff",
]


def apply_filters(
    rows: List[Dict[str, Any]],
    date_range: Optional[Any] = None,
    action: Optional[str] = None,
    resource: Optional[str] = None,
    category: Optional[str] = None,
    actor: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Filter `rows` by the audit-log filter controls. Pure.

    Each keyword argument is applied only when not None. `date_range`, if
    given, is a `(start, end)` pair compared against `created_at`
    inclusively.
    """
    filtered = rows
    if action is not None:
        filtered = [r for r in filtered if r.get("action") == action]
    if resource is not None:
        filtered = [r for r in filtered if r.get("resource") == resource]
    if category is not None:
        filtered = [r for r in filtered if r.get("category") == category]
    if actor is not None:
        filtered = [r for r in filtered if r.get("actor_id") == actor]
    if date_range is not None:
        start, end = date_range
        filtered = [
            r
            for r in filtered
            if r.get("created_at") is not None and start <= r["created_at"] <= end
        ]
    return filtered


def layout() -> html.Div:
    return html.Div(
        [
            html.H1("Audit Log"),
            html.Div(
                [
                    dcc.DatePickerRange(id="audit-filter-date-range"),
                    dcc.Input(id="audit-filter-action", placeholder="action"),
                    dcc.Input(id="audit-filter-resource", placeholder="resource"),
                    dcc.Input(id="audit-filter-category", placeholder="category"),
                    dcc.Input(id="audit-filter-actor", placeholder="actor"),
                ]
            ),
            dash_table.DataTable(
                id="audit-log-table",
                columns=[{"name": c, "id": c} for c in DISPLAY_COLUMNS],
                data=[],
                sort_by=[{"column_id": "created_at", "direction": "desc"}],
                page_action="native",
                page_size=25,
                sort_action="native",
            ),
        ]
    )
