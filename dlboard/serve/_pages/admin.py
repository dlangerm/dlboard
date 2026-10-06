"""Administer the backend for the experiment server."""

import dash
from dash import html
from dash.dcc import Store

from dlboard.serve._pages._simple_admin_page import ADMIN_INITIAL_TAB_ID, PAGE_ADMIN_ID


def layout(tab: str = "trash", **_query: str) -> list[html.Div | Store]:
    return [Store(id=ADMIN_INITIAL_TAB_ID, data=tab), html.Div(id=PAGE_ADMIN_ID)]


dash.register_page(__name__, path="/admin")  # pyright: ignore[reportUnknownMemberType]
