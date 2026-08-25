"""Nexus Finance — minimal Dash application shell (feature 16).

Constructs the single `dash.Dash` application object every page in
`dashboard/pages/` registers into via `dash.register_page`. Without this
module none of those pages render — nothing else in the tree builds the
`dash.Dash` instance.

Scope is deliberately narrow: page registration (`use_pages=True`) plus a
sidebar built by iterating `dash.page_registry`. No theming, no CSS/assets,
no auth gating, no callbacks, and no page-specific logic — those are out
of scope for this feature.

Downstream-stable ids (feature 11 depends on these; do not rename):
  - sidebar container: `id="app-sidebar"`
  - each nav link: `id={"type": "nav-link", "path": <registry entry path>}`
  - each link's badge slot: `id={"type": "nav-badge", "path": <same path>}`,
    rendered empty here. Feature 11 attaches a callback targeting
    `{"type": "nav-badge", "path": "/approval-queue"}` without editing this
    module.
"""

from __future__ import annotations

from typing import Any, Mapping

import dash
from dash import html


def build_sidebar(registry: Mapping[str, Mapping[str, Any]]) -> html.Div:
    """Build the sidebar nav from a Dash page registry mapping.

    Pure — no database access, no side effects. Iterates `registry`; never
    hardcodes a page entry, so pages added by any feature (before or after
    this one) appear automatically.
    """
    links = []
    for entry in registry.values():
        path = entry["path"]
        links.append(
            html.Div(
                [
                    html.A(
                        entry.get("name", path),
                        href=path,
                        id={"type": "nav-link", "path": path},
                    ),
                    html.Span(id={"type": "nav-badge", "path": path}),
                ]
            )
        )
    return html.Div(links, id="app-sidebar")


app = dash.Dash(__name__, use_pages=True, pages_folder="pages")

app.layout = html.Div(
    [
        build_sidebar(dash.page_registry),
        dash.page_container,
    ]
)


if __name__ == "__main__":
    app.run(debug=True)
