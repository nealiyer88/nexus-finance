"""Connectors page — connected systems grouped by category (feature 16).

Renders connector status (connected, category, last_sync,
last_sync_status, last_sync_error), Connect/Disconnect controls that link
out to feature 17's OAuth entrypoints (render only — no flow here), a
manual-sync button per connector, and "Coming Soon" slots for connectors
not yet built. No entity count anywhere — deferred per Owner Decision 5 /
FOLLOW-UP 16-B (features/infrastructure/connectors-audit-infra.md); the
data lives in the SQLite graph store and `api/` has no read path to it.
"""

from __future__ import annotations

from typing import Any, Dict, List

import dash
from dash import html

dash.register_page(__name__, path="/connectors", name="Connectors")

# Providers not yet connected — rendered as "Coming Soon" slots.
COMING_SOON = [
    {"provider": "billcom", "category": "ap", "label": "Bill.com"},
    {"provider": "stripe", "category": "payments", "label": "Stripe"},
    {"provider": "gusto", "category": "payroll", "label": "Gusto"},
]


def _connector_card(connector: Dict[str, Any]) -> html.Div:
    provider = connector["provider"]
    return html.Div(
        [
            html.H3(provider),
            html.Div(f"category: {connector.get('category')}"),
            html.Div(f"connected: {connector.get('connected')}"),
            html.Div(f"last_sync: {connector.get('last_sync')}"),
            html.Div(f"last_sync_status: {connector.get('last_sync_status')}"),
            html.Div(f"last_sync_error: {connector.get('last_sync_error')}"),
            html.A("Connect", href=f"/connect/{provider}"),
            html.A("Disconnect", href=f"/disconnect/{provider}"),
            html.Button(
                "Sync now", id={"type": "connector-sync", "provider": provider}
            ),
        ],
        id={"type": "connector-card", "provider": provider},
    )


def _coming_soon_card(entry: Dict[str, str]) -> html.Div:
    return html.Div(
        [html.H3(entry["label"]), html.Div("Coming Soon")],
        id={"type": "connector-card", "provider": entry["provider"]},
    )


def build_category_groups(connectors: List[Dict[str, Any]]) -> List[html.Div]:
    """Return one group component per distinct category in `connectors`.

    Pure. Each group carries `id={"type": "connector-category", "category":
    <category>}` and contains one `{"type": "connector-card", "provider":
    ...}` per connector in that category.
    """
    by_category: Dict[str, List[Dict[str, Any]]] = {}
    for connector in connectors:
        by_category.setdefault(connector["category"], []).append(connector)

    groups = []
    for category, items in by_category.items():
        groups.append(
            html.Div(
                [html.H2(category)] + [_connector_card(c) for c in items],
                id={"type": "connector-category", "category": category},
            )
        )
    return groups


def layout() -> html.Div:
    # Render-time connector list is fetched by the client against
    # `GET /connectors/`; this shell has no server-side data fetch wired
    # up (out of scope — see FOLLOW-UP items). Empty until wired.
    connectors: List[Dict[str, Any]] = []
    return html.Div(
        [
            html.H1("Connectors"),
            html.Div(build_category_groups(connectors)),
            html.H2("Coming Soon"),
            html.Div([_coming_soon_card(entry) for entry in COMING_SOON]),
        ]
    )
