import dash
from dash import html, dcc

dash.register_page(__name__, path="/connectors", name="Connectors")

layout = html.Div([
    html.H1("Connectors"),
    # TODO: QuickBooks, Ruddr connection status and sync controls
])
