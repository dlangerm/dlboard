"""Plugin for a basic homepage."""

import typing

import dash_mantine_components as dmc  # pyright: ignore[reportMissingTypeStubs]
from dash import Dash, Input, Output, State, dcc

from dltrack import models
from dltrack.models import constants
from dltrack.plugins.utilities import get_data_store

PROJECT_LIST_ID: typing.Final = "project-list-id"
NEW_PROJECT_BUTTON_ID: typing.Final = "new-project-button"
NEW_PROJECT_NAME_ID: typing.Final = "new-project-name"


def _list_projects(store: models.DataStore[...]) -> list[dmc.Card]:
    return [
        dmc.Card(
            [
                dmc.CardSection(
                    [dmc.Title(project.name, fw=500)],
                    inheritPadding=True,
                ),
                dmc.CardSection(
                    [dmc.Text(project.description)],
                    inheritPadding=True,
                ),
                dmc.CardSection(
                    dcc.Link("Open", href=f"/project/{project.id}"),
                    inheritPadding=True,
                ),
            ],
            withBorder=True,
            padding="sm",
            m="sm",
        )
        for project in store.get_projects()
    ]


def plug(app: Dash) -> None:
    """Render a basic homepage."""

    @app.callback(Output(constants.PAGE_HOME_ID, component_property="children"))  # pyright: ignore[reportUnknownMemberType]
    def layout_homepage() -> dmc.Container:
        return dmc.Container(
            children=[
                dmc.Flex(
                    [
                        dmc.TextInput(id=NEW_PROJECT_NAME_ID, placeholder="New Project Name"),
                        dmc.Button(id=NEW_PROJECT_BUTTON_ID, n_clicks=0, children="Submit"),
                    ]
                ),
                dmc.Divider(),
                dmc.Flex(id=PROJECT_LIST_ID, justify="flex-start"),
            ],
        )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(component_id=PROJECT_LIST_ID, component_property="children"),
        Input(component_id=NEW_PROJECT_BUTTON_ID, component_property="n_clicks", allow_optional=True),
        State(component_id=NEW_PROJECT_NAME_ID, component_property="value", allow_optional=True),
    )
    def create_project(n_clicks: int, new_project_name: str | None) -> list[dmc.Card]:
        store = get_data_store()
        if n_clicks > 0:
            if not new_project_name:
                msg = "Project name cannot be empty"
                raise ValueError(msg)
            store.create_project(models.NewProject(name=new_project_name, description="New Project"))
        return _list_projects(store)
