"""The homepage for dltrack."""

from __future__ import annotations

import typing

import dash
from dash import Input, Output, State, callback, dcc, html

from dltrack._models.project import NewProject
from dltrack._server.backend.common import get_store

if typing.TYPE_CHECKING:
    from dltrack._models.data_store import DataStore

PROJECT_LIST_ID: typing.Final = "project-list-id"
NEW_PROJECT_BUTTON_ID: typing.Final = "new-project-button"
NEW_PROJECT_NAME_ID: typing.Final = "new-project-name"

dash.register_page(__name__, path="/")


def list_projects(store: DataStore) -> list[html.Div]:
    return [
        html.Div(dcc.Link(project.name, href=f"/project/{project.id}"))
        for project in list(store.get_projects())
    ]


def layout() -> html.Div:
    return html.Div(
        [
            html.H4("Jump to Project"),
            dcc.Loading(html.Div(id=PROJECT_LIST_ID)),
            dcc.Input(id=NEW_PROJECT_NAME_ID, type="text", placeholder="New Project Name"),
            html.Button(id=NEW_PROJECT_BUTTON_ID, n_clicks=0, children="Submit"),
        ]
    )


@callback(
    Output(component_id=PROJECT_LIST_ID, component_property="children"),
    Output(component_id=NEW_PROJECT_NAME_ID, component_property="value"),
    Input(component_id=NEW_PROJECT_BUTTON_ID, component_property="n_clicks"),
    State(component_id=NEW_PROJECT_NAME_ID, component_property="value"),
)
def create_project(n_clicks: int, new_project_name: str) -> tuple[list[html.Div], str]:
    store = get_store()
    if n_clicks > 0:
        if not new_project_name:
            msg = "Project name cannot be empty"
            raise ValueError(msg)
        store.create_project(NewProject(name=new_project_name, description="New Project"))
    return list_projects(store), ""
