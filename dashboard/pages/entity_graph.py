import dash
from dash import html, dcc

dash.register_page(__name__, path="/entity-graph", name="Entity Graph")

layout = html.Div([
    html.H1("Entity Graph"),
    # TODO: D3/Cytoscape entity relationship visualization
])
