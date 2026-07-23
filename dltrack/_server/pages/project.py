"""The project page for dltrack."""

import dash
from dash import html

from dltrack._server.backend.common import get_store


def title(project_id: int) -> str:
    return f"Project: {project_id}"


def layout(project_id: int) -> html.Div:
    store = get_store()
    project = store.get_project(project_id)
    return html.Div(
        [
            html.H1(f"This the page for project {project_id}"),
            html.Div(f"This is our page for the project {project.description=}."),
        ]
    )


dash.register_page(__name__, path_template="/project/<project_id>", title=title)
