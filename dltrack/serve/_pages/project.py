"""The project page for dltrack."""

from __future__ import annotations

import dash
from dash import dcc, html

from dltrack.models import constants


def layout(project_id: str) -> list[html.Div | dcc.Store]:
    return [
        html.Div(id=constants.PAGE_PROJECT_ID),
        dcc.Store(id=constants.STATE_PROJECT_ID, data=int(project_id)),
    ]


dash.register_page(__name__, path_template="/project/<project_id>")  # pyright: ignore[reportUnknownMemberType]
