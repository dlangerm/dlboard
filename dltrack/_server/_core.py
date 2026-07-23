"""Core components for the dltrack server."""

from __future__ import annotations

import typing
from pathlib import Path  # noqa: TC003

import dash
from dash import Dash, Input, Output, State, callback, dcc, html
from pydantic_settings import BaseSettings

from dltrack import NewProject
from dltrack._server.backend.store import sqllite_store

if typing.TYPE_CHECKING:
    from dltrack._server.backend.store.store_protocol import DataStore

PROJECT_LIST_ID: typing.Final = "project-list-id"
NEW_PROJECT_BUTTON_ID: typing.Final = "new-project-button"
NEW_PROJECT_NAME_ID: typing.Final = "new-project-name"


class AppSettings(BaseSettings):
    """Environment variables."""

    store_type: typing.Literal["sqllite"] = "sqllite"
    """The store type"""

    sqllite_location: Path | None = None
    """The path to the sqllite storage location."""


def get_store() -> DataStore:
    settings = AppSettings()
    match settings.store_type:
        case "sqllite":
            assert settings.sqllite_location is not None, "sqllite location must be given."
            return sqllite_store.SQLLiteStore.get_or_create(loc=settings.sqllite_location)
    raise NotImplementedError(settings.store_type)


def list_projects(store: DataStore) -> list[html.Span]:
    return [
        html.Span(dcc.Link(project.name, href=f"/project/{project.id}"))
        for project in list(store.get_projects())
    ]


def app() -> dash.Dash:
    app: typing.Final = Dash(__name__, use_pages=True)
    app.layout = html.Div(
        [
            html.H1("DLTrack"),
            html.Span(dcc.Link("Home", href="/")),
            html.Span(" "),
            html.Span(dcc.Link("Admin", href="/admin")),
            dcc.Input(id=NEW_PROJECT_NAME_ID, type="text", placeholder="New Project Name"),
            html.Button(id=NEW_PROJECT_BUTTON_ID, n_clicks=0, children="Submit"),
            html.H4("Jump to Project"),
            dcc.Loading(html.Div(id=PROJECT_LIST_ID)),
            dash.page_container,
        ]
    )
    return app


@callback(
    Output(component_id=PROJECT_LIST_ID, component_property="children"),
    Output(component_id=NEW_PROJECT_NAME_ID, component_property="value"),
    Input(component_id=NEW_PROJECT_BUTTON_ID, component_property="n_clicks"),
    State(component_id=NEW_PROJECT_NAME_ID, component_property="value"),
)
def create_project(n_clicks: int, new_project_name: str) -> tuple[list[html.Span], str]:
    store = get_store()
    if n_clicks > 0:
        if not new_project_name:
            msg = "Project name cannot be empty"
            raise ValueError(msg)
        store.create_project(NewProject(name=new_project_name, description="New Project"))
    return list_projects(store), ""
