import dash
from dash import html, dcc

dash.register_page(__name__, path="/audit-log", name="Audit Log")

layout = html.Div([
    html.H1("Audit Log"),
    # TODO: Filterable audit trail table
])
