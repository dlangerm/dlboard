"""The project page for dltrack."""

import dash
from dash import html


def title(project_id: int) -> str:
    return f"Project: {project_id}"


dash.register_page(__name__, path_template="/project/<project_id>", title=title)


def layout(project_id: int) -> html.Div:
    return html.Div(
        [
            html.H1(f"This the page for project {project_id}"),
            html.Div("This is our page content."),
        ]
    )
