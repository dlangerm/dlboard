"""Administer the backend for the experiment server."""

import dash
from dash import html

from dltrack.models import constants


def layout() -> list[html.Div]:
    return [html.Div(id=constants.PAGE_ADMIN_ID)]


dash.register_page(__name__, path="/admin")  # pyright: ignore[reportUnknownMemberType]
