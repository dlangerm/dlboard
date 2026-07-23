"""The homepage for dltrack."""

import dash
from dash import html

from dltrack.models import constants


def layout() -> list[html.Div]:
    return [html.Div(id=constants.PAGE_HOME_ID)]


dash.register_page(__name__, path="/", layout=layout)  # pyright: ignore[reportUnknownMemberType]
