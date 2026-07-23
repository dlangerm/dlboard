"""Plugin for a basic homepage."""

import typing

from dash import Dash, Input, Output, State, dcc, html

from dltrack import models
from dltrack.models import constants
from dltrack.plugins.utilities import get_data_store

PROJECT_LIST_ID: typing.Final = "project-list-id"
NEW_PROJECT_BUTTON_ID: typing.Final = "new-project-button"
NEW_PROJECT_NAME_ID: typing.Final = "new-project-name"


def _list_projects(store: models.DataStore[...]) -> list[html.Div]:
    return [
        html.Div(dcc.Link(project.name, href=f"/project/{project.id}"))
        for project in list(store.get_projects())
    ]


def plug(app: Dash) -> None:
    """Render a basic homepage."""

    @app.callback(Output(constants.PAGE_HOME_ID, component_property="children"))  # pyright: ignore[reportUnknownMemberType]
    def layout_homepage() -> html.Div:
        return html.Div(
            [
                html.Div("Simple Homepage"),
                html.Div(id=PROJECT_LIST_ID),
                dcc.Input(id=NEW_PROJECT_NAME_ID, type="text", placeholder="New Project Name"),
                html.Button(id=NEW_PROJECT_BUTTON_ID, n_clicks=0, children="Submit"),
            ]
        )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(component_id=PROJECT_LIST_ID, component_property="children"),
        Input(component_id=NEW_PROJECT_BUTTON_ID, component_property="n_clicks"),
        State(component_id=NEW_PROJECT_NAME_ID, component_property="value"),
    )
    def create_project(n_clicks: int, new_project_name: str) -> list[html.Div]:
        store = get_data_store()
        if n_clicks > 0:
            if not new_project_name:
                msg = "Project name cannot be empty"
                raise ValueError(msg)
            store.create_project(models.NewProject(name=new_project_name, description="New Project"))
        return _list_projects(store)
