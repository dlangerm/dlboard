"""The homepage: every project as a card, plus an inline "new project" field."""

import typing

import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, no_update
from dash.development.base_component import Component
from dash.exceptions import PreventUpdate

from dlboard import models
from dlboard.serve import Icon, get_data_store, icon
from dlboard.serve._pages._dash_helpers import entity_card
from dlboard.serve._pages._onboarding import first_project_snippet, onboarding_card
from dlboard.serve._url import relative_path

PROJECT_LIST_ID: typing.Final = "project-list-id"
NEW_PROJECT_BUTTON_ID: typing.Final = "new-project-button"
NEW_PROJECT_NAME_ID: typing.Final = "new-project-name"
PROJECT_COUNT_ID: typing.Final = "project-count"


def _project_count(n: int) -> str:
    return "1 project" if n == 1 else f"{n} projects"


def _project_grid(store: models.DataStore[...]) -> tuple[Component, int]:
    """The project cards (or an empty state), and how many projects there are."""
    projects = list(store.get_projects())
    if not projects:
        return onboarding_card(
            title="No projects yet — log your first run", snippet=first_project_snippet()
        ), 0
    stats = store.get_project_stats()
    cards = [_project_card(p, stats.get(p.id, models.ProjectStats())) for p in projects]
    return dmc.SimpleGrid(cards, cols={"base": 1, "sm": 2, "lg": 3}, spacing="md"), len(projects)


def _project_card(project: models.Project, stats: models.ProjectStats) -> Component:
    return entity_card(
        title=project.name,
        description=project.description,
        href=relative_path(f"/project/{project.id}"),
        stats=stats,
        class_name="project-card",
    )


def render_homepage() -> dmc.Container:
    """The whole homepage, rendered in the page's own `layout()` -- no callback round trip."""
    grid, count = _project_grid(get_data_store())
    return dmc.Container(
        [
            dmc.Group(
                [
                    dmc.Stack(
                        [
                            dmc.Title("Projects", order=2),
                            dmc.Text(_project_count(count), id=PROJECT_COUNT_ID, c="dimmed", size="sm"),
                        ],
                        gap=2,
                    ),
                    dmc.Group(
                        [
                            dmc.TextInput(
                                id=NEW_PROJECT_NAME_ID,
                                placeholder="New project name",
                                w=240,
                                **typing.cast("dict[str, typing.Any]", {"aria-label": "New project name"}),
                            ),
                            dmc.Button("Create", id=NEW_PROJECT_BUTTON_ID, leftSection=icon(Icon.ADD)),
                        ],
                        gap="xs",
                        align="flex-start",
                        wrap="nowrap",
                    ),
                ],
                justify="space-between",
                align="flex-end",
                mb="lg",
            ),
            dmc.Box(grid, id=PROJECT_LIST_ID),
        ],
        size="lg",
        py="xl",
    )


def register(app: Dash) -> None:
    """Create a project from the inline field (button or Enter), then refresh the list in place."""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(PROJECT_LIST_ID, "children"),
        Output(PROJECT_COUNT_ID, "children"),
        Output(NEW_PROJECT_NAME_ID, "value"),
        Output(NEW_PROJECT_NAME_ID, "error"),
        Input(NEW_PROJECT_BUTTON_ID, "n_clicks"),
        Input(NEW_PROJECT_NAME_ID, "n_submit"),
        State(NEW_PROJECT_NAME_ID, "value"),
        prevent_initial_call=True,
    )
    def create_project(
        n_clicks: int | None, n_submit: int | None, name: str | None
    ) -> tuple[typing.Any, ...]:
        if not n_clicks and not n_submit:
            raise PreventUpdate
        if not name or not name.strip():
            return no_update, no_update, no_update, "Give the project a name"
        store = get_data_store()
        store.create_project(models.NewProject(name=name.strip(), description=""))
        grid, count = _project_grid(store)
        return grid, _project_count(count), "", None
