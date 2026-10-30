"""The project page for dlboard."""

from __future__ import annotations

from typing import TYPE_CHECKING

import dash
from dash import dcc

from dlboard.serve import _constants as constants
from dlboard.serve._pages._pins._pinned_runs import pinned_runs_shells
from dlboard.serve._pages._simple_project_page import render_project_page

if TYPE_CHECKING:
    from dash.development.base_component import Component


def layout(project_id: str, **_query: str) -> list[Component]:
    """
    The project page, with its URL's query parameters passed in as keyword arguments by Dash and left to the sidebar.

    The pinned runs' filters (`?pins_color=` and the rest) are read from the address by the sidebar when it is drawn
    (`PinFilter`), so the page itself has no use for them; they must not be refused, though.
    """
    return [
        render_project_page(int(project_id)),
        dcc.Store(id=constants.STATE_PROJECT_ID, data=int(project_id)),
        *pinned_runs_shells(int(project_id)),
    ]


dash.register_page(__name__, path_template="/project/<project_id>")
