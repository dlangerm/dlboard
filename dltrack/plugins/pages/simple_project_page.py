"""A project page."""

from __future__ import annotations

import typing

import dash_mantine_components as dmc  # pyright: ignore[reportMissingTypeStubs]
from dash import Dash, Input, Output, State

from dltrack.models import NewExperiment, constants
from dltrack.plugins.utilities import get_data_store

PROJECT_ID: typing.Final = "project-id"
EXP_LIST_ID: typing.Final = "experiment-list-id"
NEW_EXP_BUTTON_ID: typing.Final = "new-experiment-button"
NEW_EXP_NAME_ID: typing.Final = "new-experiment-name"

_CARD_COLORS = ["indigo", "teal", "grape", "orange", "cyan", "pink"]


def _experiment_card(experiment_id: int, color: str) -> dmc.Card:
    return dmc.Card(
        [
            dmc.CardSection(
                dmc.Box(h=6, bg=f"{color}.5"),
            ),
            dmc.Group(
                [
                    dmc.ThemeIcon("E", size="lg", radius="xl", color=color, variant="light"),
                    dmc.Title(f"Experiment {experiment_id}", order=4, fw=600),
                ],
                gap="sm",
                mt="md",
            ),
            dmc.Anchor(
                dmc.Button("Open experiment", variant="light", color=color, fullWidth=True, mt="md"),
                href=f"/experiment/{experiment_id}",
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
        [_experiment_card(e.id, _CARD_COLORS[i % len(_CARD_COLORS)]) for i, e in enumerate(experiments)],
        cols=3,
        spacing="md",
    )


def plug(app: Dash) -> None:
    """Plugin."""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.PAGE_PROJECT_ID, component_property="children"),
        State(constants.STATE_PROJECT_ID, component_property="data"),
    )
    def _layout(project_id: int) -> dmc.Container:
        return dmc.Container(
            [
                dmc.Stack(
                    [
                        dmc.Title(f"Project {project_id}", order=2, fw=700),
                        dmc.Text("Create and review experiments for this project.", c="dimmed", size="sm"),
                    ],
                    gap=2,
                    mt="lg",
                    mb="md",
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
            store.create_experiment(NewExperiment(project_id=int(project_id)))
        return _list_experiments(project_id)
