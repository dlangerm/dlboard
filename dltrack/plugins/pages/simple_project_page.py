"""A project page."""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING

import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, html

from dltrack.models import NewExperiment, constants
from dltrack.plugins.pages._actor import current_actor_id
from dltrack.plugins.pages._dataframe_helpers import experiment_display_name
from dltrack.plugins.pages._delete_confirm import (
    DeleteConfirmIds,
    register_delete_callbacks,
    render_delete_control,
)
from dltrack.plugins.pages._description_editor import (
    DescriptionEditorIds,
    register_edit_callbacks,
    render_header,
)
from dltrack.serve import get_data_store

if TYPE_CHECKING:
    from dltrack.models import Experiment

PROJECT_ID: typing.Final = "project-id"
PROJECT_HEADER_ID: typing.Final = "project-header"
EXP_LIST_ID: typing.Final = "experiment-list-id"
NEW_EXP_BUTTON_ID: typing.Final = "new-experiment-button"
NEW_EXP_NAME_ID: typing.Final = "new-experiment-name"

PROJECT_DESC_IDS = DescriptionEditorIds(
    header=PROJECT_HEADER_ID,
    edit_button="project-edit-desc-button",
    modal="project-edit-desc-modal",
    textarea="project-edit-desc-textarea",
    save="project-edit-desc-save",
    cancel="project-edit-desc-cancel",
)

PROJECT_DELETE_IDS = DeleteConfirmIds(
    button=constants.DELETE_PROJECT_BUTTON_ID,
    modal=constants.DELETE_PROJECT_MODAL_ID,
    confirm=constants.DELETE_PROJECT_CONFIRM_ID,
    cancel=constants.DELETE_PROJECT_CANCEL_ID,
)

_CARD_COLORS = ["indigo", "teal", "grape", "orange", "cyan", "pink"]


def _experiment_card(experiment: Experiment, color: str) -> dmc.Card:
    return dmc.Card(
        [
            dmc.CardSection(
                dmc.Box(h=6, bg=f"{color}.5"),
            ),
            dmc.Group(
                [
                    dmc.ThemeIcon("E", size="lg", radius="xl", color=color, variant="light"),
                    dmc.Title(experiment_display_name(experiment), order=4, fw=600),
                ],
                gap="sm",
                mt="md",
            ),
            dmc.Text(
                experiment.description or "No description",
                size="sm",
                c="dimmed",
                mt="xs",
                lineClamp=2,
            ),
            dmc.Anchor(
                dmc.Button("Open experiment", variant="light", color=color, fullWidth=True, mt="md"),
                href=f"/experiment/{experiment.id}",
                underline="never",
                refresh=False,
            ),
        ],
        withBorder=True,
        radius="md",
        padding="lg",
        shadow="sm",
    )


def _list_experiments(project_id: int) -> dmc.SimpleGrid | dmc.Center:
    store = get_data_store()
    experiments = list(store.get_experiments(project_id))
    if not experiments:
        return dmc.Center(
            dmc.Stack(
                [
                    dmc.Text("No experiments yet", fw=600, size="lg"),
                    dmc.Text("Create your first experiment above to get started.", c="dimmed", size="sm"),
                ],
                align="center",
                gap=4,
            ),
            mt="xl",
            mb="xl",
        )
    return dmc.SimpleGrid(
        [_experiment_card(e, _CARD_COLORS[i % len(_CARD_COLORS)]) for i, e in enumerate(experiments)],
        cols=3,
        spacing="md",
    )


def plug(app: Dash) -> None:
    """Plugin."""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.PAGE_PROJECT_ID, component_property="children"),
        State(constants.STATE_PROJECT_ID, component_property="data"),
    )
    def layout(project_id: int) -> dmc.Container:
        store = get_data_store()
        project = store.get_project(project_id)
        return dmc.Container(
            [
                dmc.Group(
                    [
                        html.Div(
                            id=PROJECT_HEADER_ID,
                            children=render_header(
                                PROJECT_DESC_IDS, title=project.name, description=project.description
                            ),
                            style={"flex": 1},
                        ),
                        *render_delete_control(
                            PROJECT_DELETE_IDS, label="Delete project", entity_noun="project"
                        ),
                    ],
                    justify="space-between",
                    align="flex-start",
                    style={
                        "marginTop": "var(--mantine-spacing-lg)",
                        "marginBottom": "var(--mantine-spacing-md)",
                    },
                ),
                dmc.Paper(
                    dmc.Group(
                        [
                            dmc.TextInput(
                                id=NEW_EXP_NAME_ID,
                                placeholder="New experiment name",
                                style={"flex": 1},
                                size="sm",
                            ),
                            dmc.Button(id=NEW_EXP_BUTTON_ID, n_clicks=0, children="Create experiment"),
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
                dmc.Box(id=EXP_LIST_ID),
            ],
            size="lg",
            py="xl",
        )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(component_id=EXP_LIST_ID, component_property="children"),
        Input(component_id=NEW_EXP_BUTTON_ID, component_property="n_clicks"),
        State(constants.STATE_PROJECT_ID, component_property="data"),
        State(component_id=NEW_EXP_NAME_ID, component_property="value"),
    )
    def create_experiment(
        n_clicks: int, project_id: int, new_experiment_name: str
    ) -> dmc.SimpleGrid | dmc.Center:
        if n_clicks > 0:
            if not new_experiment_name:
                msg = "Experiment name cannot be empty"
                raise ValueError(msg)
            store = get_data_store()
            store.create_experiment(
                NewExperiment(
                    project_id=int(project_id),
                    name=new_experiment_name,
                    created_by=current_actor_id(store),
                )
            )
        return _list_experiments(project_id)

    def _fetch_project_header(project_id: int) -> tuple[str, str]:
        store = get_data_store()
        project = store.get_project(project_id)
        return project.name, project.description

    def _save_project_description(project_id: int, description: str) -> tuple[str, str]:
        store = get_data_store()
        project = store.get_project(project_id)
        updated = store.update_project(project.model_copy(update={"description": description}))
        return updated.name, updated.description

    register_edit_callbacks(
        app,
        PROJECT_DESC_IDS,
        State(constants.STATE_PROJECT_ID, "data"),
        fetch=_fetch_project_header,
        save=_save_project_description,
    )

    def _delete_project(project_id: int) -> str:
        store = get_data_store()
        store.delete_project(project_id, actor_id=current_actor_id(store))
        return "/"

    register_delete_callbacks(
        app,
        PROJECT_DELETE_IDS,
        State(constants.STATE_PROJECT_ID, "data"),
        on_confirm=_delete_project,
    )
