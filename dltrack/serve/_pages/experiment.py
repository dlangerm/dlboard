"""The experiment page for dltrack."""

import dash
from dash import dcc, html
from dash.dcc import Store

from dltrack.models import constants


def layout(experiment_id: str) -> list[html.Div | dcc.Store]:

    return [
        html.Div(id=constants.PAGE_EXPERIMENT_ID),
        Store(id=constants.STATE_EXPERIMENT_ID, data=int(experiment_id)),
    ]


dash.register_page(__name__, path_template="/experiment/<experiment_id>")  # pyright: ignore[reportUnknownMemberType]
