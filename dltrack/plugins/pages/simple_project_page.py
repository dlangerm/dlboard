"""A project page."""

from __future__ import annotations

import typing

from dash import Dash, Input, Output, State, dcc, html

from dltrack.models import NewExperiment, constants
from dltrack.plugins.utilities import get_data_store

PROJECT_ID: typing.Final = "project-id"
EXP_LIST_ID: typing.Final = "experiment-list-id"
NEW_EXP_BUTTON_ID: typing.Final = "new-experiment-button"
NEW_EXP_NAME_ID: typing.Final = "new-experiment-name"


def _list_experiments(project_id: int) -> list[html.Div]:
    store = get_data_store()
    return [
        html.Div(dcc.Link(experiment.id, href=f"/experiment/{experiment.id}"))
        for experiment in list(store.get_experiments(project_id))
    ]


def plug(app: Dash) -> None:
    """Plugin."""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.PAGE_PROJECT_ID, component_property="children"),
        State(constants.STATE_PROJECT_ID, component_property="data"),
    )
    def _layout(project_id: int) -> html.Div:
        return html.Div(
            [
                html.H1(f"This the page for project {project_id}"),
                html.Div(id=EXP_LIST_ID),
                dcc.Input(id=NEW_EXP_NAME_ID, type="text", placeholder="New Project Name"),
                html.Button(id=NEW_EXP_BUTTON_ID, n_clicks=0, children="Submit"),
            ]
        )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(component_id=EXP_LIST_ID, component_property="children"),
        Input(component_id=NEW_EXP_BUTTON_ID, component_property="n_clicks"),
        State(constants.STATE_PROJECT_ID, component_property="data"),
        State(component_id=NEW_EXP_NAME_ID, component_property="value"),
    )
    def create_experiment(n_clicks: int, project_id: int, new_experiment_name: str) -> list[html.Div]:
        if n_clicks > 0:
            if not new_experiment_name:
                msg = "Experiment name cannot be empty"
                raise ValueError(msg)
            store = get_data_store()
            store.create_experiment(NewExperiment(project_id=int(project_id)))
        return _list_experiments(project_id)
