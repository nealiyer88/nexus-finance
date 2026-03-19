import dash
from dash import html, dcc

dash.register_page(__name__, path="/ar-reconciliation", name="AR Reconciliation")

layout = html.Div([
    html.H1("AR Reconciliation"),
    # TODO: Match invoices to payments, flag discrepancies
])
