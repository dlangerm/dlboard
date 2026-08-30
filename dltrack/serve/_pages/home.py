"""The homepage for dltrack."""

import dash
from dash import html

from dltrack.plugins.pages.simple_homepage import PAGE_HOME_ID


def layout() -> list[html.Div]:
    return [html.Div(id=PAGE_HOME_ID)]


dash.register_page(__name__, path="/", layout=layout)  # pyright: ignore[reportUnknownMemberType]
