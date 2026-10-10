"""The account page for dlboard."""

import dash
from dash import html

from dlboard.serve._pages._account_page import PAGE_ACCOUNT_ID, render_account_page


def layout() -> html.Div:
    return html.Div(render_account_page(), id=PAGE_ACCOUNT_ID)


dash.register_page(__name__, path="/account")
