"""Administer the backend for the experiment server."""

import dash
from dash import html

from dltrack.serve._pages._simple_admin_page import PAGE_ADMIN_ID


def layout() -> list[html.Div]:
    return [html.Div(id=PAGE_ADMIN_ID)]


dash.register_page(__name__, path="/admin")  # pyright: ignore[reportUnknownMemberType]
