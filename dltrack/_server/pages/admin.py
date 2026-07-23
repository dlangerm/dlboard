"""Administer the backend for the experiment server."""

import dash
from dash import html

dash.register_page(__name__, path="/admin")


def layout() -> html.Div:
    return html.Div(
        [
            html.H1("Administer the server"),
            html.Div("This is our page content."),
        ]
    )
