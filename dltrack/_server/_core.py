"""Core components for the dltrack server."""

from __future__ import annotations

import typing

import dash
from dash import Dash, dcc, html

app: typing.Final = Dash(__name__, use_pages=True)
app.layout = html.Div(
    [
        html.H1("DLTrack"),
        html.Span(dcc.Link("Home", href="/")),
        html.Span(" "),
        html.Span(dcc.Link("Admin", href="/admin")),
        dash.page_container,
    ]
)

import dltrack._server.backend.routes  # noqa: E402, F401 (app must be defined firstt)
