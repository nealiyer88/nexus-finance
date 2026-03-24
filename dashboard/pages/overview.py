import dash
from dash import html, dcc

dash.register_page(__name__, path="/", name="Overview")

layout = html.Div([
    html.H1("Overview"),
    # TODO: KPI cards, revenue summary, AR aging
])
