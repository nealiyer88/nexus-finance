import dash
from dash import html, dcc

dash.register_page(__name__, path="/approval-queue", name="Approval Queue")

layout = html.Div([
    html.H1("Approval Queue"),
    # TODO: Pending approvals table with approve/reject actions
])
