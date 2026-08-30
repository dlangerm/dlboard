"""The project page for dltrack."""

from __future__ import annotations

import dash
from dash import dcc, html

from dltrack.models import constants
from dltrack.plugins.pages.simple_project_page import PAGE_PROJECT_ID


def layout(project_id: str) -> list[html.Div | dcc.Store]:
    return [
        html.Div(id=PAGE_PROJECT_ID),
        dcc.Store(id=constants.STATE_PROJECT_ID, data=int(project_id)),
    ]


dash.register_page(__name__, path_template="/project/<project_id>")  # pyright: ignore[reportUnknownMemberType]
