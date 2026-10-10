"""The project page for dlboard."""

from __future__ import annotations

from typing import TYPE_CHECKING

import dash
from dash import dcc

from dlboard.serve import _constants as constants
from dlboard.serve._pages._simple_project_page import render_project_page

if TYPE_CHECKING:
    import dash_mantine_components as dmc


def layout(project_id: str) -> list[dmc.Container | dcc.Store]:
    return [
        render_project_page(int(project_id)),
        dcc.Store(id=constants.STATE_PROJECT_ID, data=int(project_id)),
    ]


dash.register_page(__name__, path_template="/project/<project_id>")
