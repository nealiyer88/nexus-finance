"""Entity Registry Browser (feature 14).

A searchable, filterable table of canonical entities, an expandable
detail row (aliases grouped by `entity_aliases.category`, system
references), and a Plotly cross-category relationship visualization
over `entity_edges` for the selected entity. `dash-cytoscape` is not
installed and is out of scope — the network view below is hand-built
from `go.Scatter` traces.

The layout below builds with no database present: no connection, no
query, no store file touched at import or layout-build time (matching
`dashboard/pages/approval_queue.py`'s convention). All data access
lives inside `@callback` bodies, which call `api.routers.entities`'s
`list_entities` / `get_entity_detail` directly — the same functions
the `/entities/` and `/entities/{canonical_id}` routes wrap — obtaining
their SQLite connection exclusively through `core.matching.
pending_store.get_connection`.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import dash
import plotly.graph_objects as go
from dash import Input, Output, State, callback, dash_table, dcc, html

from api.middleware.tenant import DEFAULT_TENANT_ID
from api.routers.entities import get_entity_detail, list_entities
from core.matching.pending_store import get_connection

dash.register_page(__name__, path="/entity-graph", name="Entity Graph")

# Exact column-id set required by the feature's acceptance criteria.
TABLE_COLUMNS: List[Dict[str, str]] = [
    {"name": "Canonical ID", "id": "canonical_id"},
    {"name": "Canonical Name", "id": "canonical_name"},
    {"name": "Entity Type", "id": "entity_type"},
    {"name": "Entity Category", "id": "entity_category"},
    {"name": "Confidence", "id": "confidence"},
    {"name": "Alias Count", "id": "alias_count"},
    {"name": "Source Categories", "id": "source_categories"},
]


def _resolve_tenant_id() -> str:
    return os.environ.get("NEXUS_TENANT_ID", DEFAULT_TENANT_ID)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def row_for_table(entity: Dict[str, Any]) -> Dict[str, Any]:
    """Shape one `list_entities` item for the DataTable — the only
    change is joining `source_categories` into a display string."""
    row = dict(entity)
    row["source_categories"] = ", ".join(entity.get("source_categories") or [])
    return row


def build_detail_view(detail: Dict[str, Any]) -> html.Div:
    """Build the expandable detail view for one entity. Pure — no I/O."""
    alias_children: List[Any] = []
    for category in sorted(detail.get("aliases", {})):
        values = detail["aliases"][category]
        alias_children.append(
            html.Div(
                f"{category}: {', '.join(values)}",
                id={"type": "entity-alias-group", "category": category},
            )
        )

    ref_children = [
        html.Div(
            f"{ref['source']} ({ref['category']}): {ref['external_id']}",
            id={"type": "entity-system-ref", "source": ref["source"]},
        )
        for ref in detail.get("system_references", [])
    ]

    return html.Div(
        [
            html.H3("Entity detail", id="entity-detail-heading"),
            html.Div(
                f"{detail['canonical_name']} ({detail['canonical_id']})",
                id="entity-detail-name",
            ),
            html.Div(alias_children, id="entity-detail-aliases"),
            html.Div(ref_children, id="entity-detail-system-references"),
        ]
    )


def build_relationship_figure(
    canonical_id: Optional[str],
    edges: List[Dict[str, Any]],
) -> go.Figure:
    """Build a Plotly cross-category relationship figure for
    `canonical_id` and its `entity_edges` neighbors. Pure — no I/O.

    The selected entity sits at the origin; each neighbor is placed on
    a unit circle around it, connected by a line trace labeled with the
    edge's `relationship` and category pair. An empty `edges` list (or
    no `canonical_id`) yields an empty, but valid, figure.
    """
    fig = go.Figure()
    if not canonical_id or not edges:
        fig.update_layout(title="No relationships to display")
        return fig

    import math

    node_x = [0.0]
    node_y = [0.0]
    node_text = [canonical_id]

    n = len(edges)
    for i, edge in enumerate(edges):
        angle = (2 * math.pi * i) / n
        x, y = math.cos(angle), math.sin(angle)
        other = (
            edge["target_node"] if edge["source_node"] == canonical_id else edge["source_node"]
        )
        fig.add_trace(
            go.Scatter(
                x=[0.0, x],
                y=[0.0, y],
                mode="lines",
                line=dict(width=max(edge.get("weight") or 0.5, 0.5) * 2),
                hovertext=(
                    f"{edge['relationship']} "
                    f"({edge['source_category']}↔{edge['target_category']})"
                ),
                showlegend=False,
            )
        )
        node_x.append(x)
        node_y.append(y)
        node_text.append(other)

    fig.add_trace(
        go.Scatter(
            x=node_x,
            y=node_y,
            mode="markers+text",
            text=node_text,
            textposition="top center",
            marker=dict(size=14),
            showlegend=False,
        )
    )
    fig.update_layout(title=f"Relationships for {canonical_id}")
    return fig


# ---------------------------------------------------------------------------
# Layout — pure at build time, no database access.
# ---------------------------------------------------------------------------


def layout() -> html.Div:
    return html.Div(
        [
            html.H1("Entity Graph"),
            html.Div(
                [
                    dcc.Input(
                        id="entity-search-input",
                        placeholder="Search entities (name or alias)…",
                        type="text",
                    ),
                    dcc.Dropdown(
                        id="entity-filter-entity-type",
                        options=[
                            {"label": t, "value": t}
                            for t in (
                                "client",
                                "vendor",
                                "project",
                                "pl_unit",
                                "cost_center",
                                "contract",
                                "person",
                            )
                        ],
                        placeholder="entity type",
                    ),
                    dcc.Dropdown(
                        id="entity-filter-entity-category",
                        options=[
                            {"label": "Organization", "value": "organization"},
                            {"label": "Person", "value": "person"},
                        ],
                        placeholder="entity category",
                    ),
                    dcc.Input(
                        id="entity-filter-confidence-min",
                        type="number",
                        placeholder="min confidence",
                    ),
                    dcc.Input(
                        id="entity-filter-confidence-max",
                        type="number",
                        placeholder="max confidence",
                    ),
                    dcc.Input(
                        id="entity-filter-source-category",
                        placeholder="source category (accounting/psa)",
                    ),
                ]
            ),
            dash_table.DataTable(
                id="entity-table",
                columns=TABLE_COLUMNS,
                data=[],
                page_action="native",
                page_size=25,
                sort_action="native",
            ),
            html.Div(id="entity-detail"),
            dcc.Graph(id="entity-relationship-graph"),
        ]
    )


# ---------------------------------------------------------------------------
# Callbacks — all database access lives here.
# ---------------------------------------------------------------------------


@callback(
    Output("entity-table", "data"),
    Input("_pages_location", "pathname"),
    Input("entity-search-input", "value"),
    Input("entity-filter-entity-type", "value"),
    Input("entity-filter-entity-category", "value"),
    Input("entity-filter-confidence-min", "value"),
    Input("entity-filter-confidence-max", "value"),
    Input("entity-filter-source-category", "value"),
)
def refresh_entity_table(
    _pathname: Optional[str],
    q: Optional[str],
    entity_type: Optional[str],
    entity_category: Optional[str],
    min_confidence: Optional[float],
    max_confidence: Optional[float],
    source_category: Optional[str],
) -> List[Dict[str, Any]]:
    tenant_id = _resolve_tenant_id()
    with get_connection() as conn:
        result = list_entities(
            conn,
            tenant_id,
            entity_type=entity_type or None,
            entity_category=entity_category or None,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            source_category=source_category or None,
            q=q or None,
            limit=None,
        )
    return [row_for_table(item) for item in result["items"]]


@callback(
    Output("entity-detail", "children"),
    Output("entity-relationship-graph", "figure"),
    Input("entity-table", "active_cell"),
    State("entity-table", "data"),
)
def show_entity_detail(active_cell: Optional[dict], table_data: Optional[List[Dict[str, Any]]]):
    if not active_cell or not table_data:
        return html.Div("Select a row to view details."), build_relationship_figure(None, [])

    row = table_data[active_cell["row"]]
    canonical_id = row.get("canonical_id")
    tenant_id = _resolve_tenant_id()
    with get_connection() as conn:
        detail = get_entity_detail(conn, canonical_id, tenant_id)
    if detail is None:
        return html.Div("Entity not found."), build_relationship_figure(None, [])
    return build_detail_view(detail), build_relationship_figure(canonical_id, detail["edges"])
