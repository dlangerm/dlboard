"""Plugin for a basic homepage."""

import typing

import dash_mantine_components as dmc
from dash import Dash, Input, Output, State

from dltrack import models
from dltrack.models import constants
from dltrack.plugins.utilities import get_data_store

PROJECT_LIST_ID: typing.Final = "project-list-id"
NEW_PROJECT_BUTTON_ID: typing.Final = "new-project-button"
NEW_PROJECT_NAME_ID: typing.Final = "new-project-name"

_CARD_COLORS = ["indigo", "teal", "grape", "orange", "cyan", "pink"]


def _project_card(project: models.Project, color: str) -> dmc.Card:
    return dmc.Card(
        [
            dmc.CardSection(
                dmc.Box(h=6, bg=f"{color}.5"),
            ),
            dmc.Group(
                [
                    dmc.ThemeIcon(
                        project.name[:1].upper(), size="lg", radius="xl", color=color, variant="light"
                    ),
                    dmc.Title(project.name, order=4, fw=600),
                ],
                gap="sm",
                mt="md",
            ),
            dmc.Text(
                project.description or "No description",
                size="sm",
                c="dimmed",
                mt="xs",
                lineClamp=2,
            ),
            dmc.Anchor(
                dmc.Button("Open project", variant="light", color=color, fullWidth=True, mt="md"),
                href=f"/project/{project.id}",
                underline="never",
            ),
        ],
        withBorder=True,
        radius="md",
        padding="lg",
        shadow="sm",
        style={"transition": "transform 120ms ease, box-shadow 120ms ease"},
        className="project-card",
    )


def _list_projects(store: models.DataStore[...]) -> dmc.SimpleGrid | dmc.Center:
    projects = list(store.get_projects())
    if not projects:
        return dmc.Center(
            dmc.Stack(
                [
                    dmc.Text("No projects yet", fw=600, size="lg"),
                    dmc.Text("Create your first project above to get started.", c="dimmed", size="sm"),
                ],
                align="center",
                gap=4,
            ),
            mt="xl",
            mb="xl",
        )
    return dmc.SimpleGrid(
        [_project_card(p, _CARD_COLORS[i % len(_CARD_COLORS)]) for i, p in enumerate(projects)],
        cols={"base": 1, "sm": 2, "lg": 3},
        spacing="md",
    )


def plug(app: Dash) -> None:
    """Render a basic homepage."""

    @app.callback(Output(constants.PAGE_HOME_ID, component_property="children"))  # pyright: ignore[reportUnknownMemberType]
    def layout_homepage() -> dmc.Container:
        return dmc.Container(
            children=[
                dmc.Stack(
                    [
                        dmc.Title("Projects", order=2, fw=700),
                        dmc.Text("Track and browse your experiment projects.", c="dimmed", size="sm"),
                    ],
                    gap=2,
                    mt="lg",
                    mb="md",
                ),
                dmc.Paper(
                    dmc.Group(
                        [
                            dmc.TextInput(
                                id=NEW_PROJECT_NAME_ID,
                                placeholder="New project name",
                                style={"flex": 1},
                                size="sm",
                            ),
                            dmc.Button(id=NEW_PROJECT_BUTTON_ID, n_clicks=0, children="Create project"),
                        ],
                        gap="sm",
                        wrap="nowrap",
                    ),
                    withBorder=True,
                    radius="md",
                    p="md",
                    mb="lg",
                ),
                dmc.Divider(mb="lg"),
                dmc.Box(id=PROJECT_LIST_ID),
            ],
            size="lg",
        )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(component_id=PROJECT_LIST_ID, component_property="children"),
        Input(component_id=NEW_PROJECT_BUTTON_ID, component_property="n_clicks"),
        State(component_id=NEW_PROJECT_NAME_ID, component_property="value"),
    )
    def create_project(n_clicks: int, new_project_name: str | None) -> dmc.SimpleGrid | dmc.Center:
        store = get_data_store()
        if n_clicks > 0:
            if not new_project_name:
                msg = "Project name cannot be empty"
                raise ValueError(msg)
            store.create_project(models.NewProject(name=new_project_name, description="New Project"))
        return _list_projects(store)
