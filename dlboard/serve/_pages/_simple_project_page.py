"""A project page: its experiments as cards, plus an inline "new experiment" field."""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING

import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, no_update
from dash.exceptions import PreventUpdate

from dlboard.models import ActivityStats, NewExperiment
from dlboard.serve import Icon, get_current_user, get_data_store, icon
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import ButtonId, ModalId
from dlboard.serve._pages._comparison._cards import comparison_cards
from dlboard.serve._pages._dash_helpers import entity_card
from dlboard.serve._pages._dataframe_helpers import experiment_display_name
from dlboard.serve._pages._delete_confirm import (
    DeleteConfirmIds,
    register_delete_callbacks,
    render_delete_control,
)
from dlboard.serve._pages._onboarding import first_experiment_snippet, onboarding_card
from dlboard.serve._pages._page_header import PageHeaderIds, page_header, register_page_header
from dlboard.serve._pages._pins._pinned_runs import register as register_pinned_runs
from dlboard.serve._pages._project_members import register_members_callbacks, render_members_section
from dlboard.serve._pages._project_sections import ProjectSlot, project_sections
from dlboard.serve._url import relative_path

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dlboard.models import DataStore, Experiment, Project


class _ProjectPage:
    """Page tag: marks a component id as belonging to `simple_project_page.py`."""


EXP_LIST_ID: typing.Final = "experiment-list-id"
NEW_EXP_BUTTON_ID: typing.Final = "new-experiment-button"
NEW_EXP_NAME_ID: typing.Final = "new-experiment-name"

PROJECT_HEADER_IDS = PageHeaderIds[_ProjectPage].of("project")

PROJECT_DELETE_IDS = DeleteConfirmIds(
    button=ButtonId[_ProjectPage]("delete-project-button"),
    modal=ModalId[_ProjectPage]("delete-project-modal"),
    confirm=ButtonId[_ProjectPage]("delete-project-confirm"),
    cancel=ButtonId[_ProjectPage]("delete-project-cancel"),
)


def _experiment_card(experiment: Experiment, stats: ActivityStats) -> Component:
    return entity_card(
        title=experiment_display_name(experiment),
        description=experiment.description,
        href=relative_path(f"/experiment/{experiment.id}"),
        stats=stats,
        class_name="experiment-card",
    )


def _experiment_grid(store: DataStore[...], project_id: int) -> Component:
    experiments = list(store.get_experiments(project_id))
    if not experiments:
        return onboarding_card(
            title="No experiments yet — log your first run", snippet=first_experiment_snippet(project_id)
        )
    stats = store.get_experiment_stats(project_id)
    return dmc.SimpleGrid(
        [_experiment_card(e, stats.get(e.id, ActivityStats())) for e in experiments],
        cols={"base": 1, "sm": 2, "lg": 3},
        spacing="md",
    )


def _visible_project(project_id: int) -> Project:
    project = get_data_store().get_project(project_id)
    if project is None:
        msg = f"Project {project_id} doesn't exist, or you don't have access to it"
        raise LookupError(msg)
    return project


def render_project_page(project_id: int) -> dmc.Container:
    """The whole project page, rendered in the page's own `layout()` -- no callback round trip."""
    store = get_data_store()
    project = store.get_project(project_id)
    if project is None:
        return dmc.Container(
            dmc.Text("This project doesn't exist, or you don't have access to it.", c="dimmed"), py="xl"
        )
    return dmc.Container(
        [
            dmc.Box(
                page_header(
                    PROJECT_HEADER_IDS,
                    title=project.name,
                    description=project.description,
                    actions=render_delete_control(
                        PROJECT_DELETE_IDS, label="Delete project", entity_noun="project", icon_only=True
                    ),
                    aside=[
                        dmc.Group(
                            [
                                dmc.TextInput(
                                    id=NEW_EXP_NAME_ID,
                                    placeholder="New experiment name",
                                    w=240,
                                    **typing.cast(
                                        "dict[str, typing.Any]", {"aria-label": "New experiment name"}
                                    ),
                                ),
                                dmc.Button("Create", id=NEW_EXP_BUTTON_ID, leftSection=icon(Icon.ADD)),
                            ],
                            gap="xs",
                            align="flex-start",
                            wrap="nowrap",
                        )
                    ],
                ),
                mb="lg",
            ),
            *project_sections(ProjectSlot.HEADER, project),
            dmc.Box(_experiment_grid(store, project_id), id=EXP_LIST_ID),
            *project_sections(ProjectSlot.BODY, project),
            comparison_cards(store, project_id),
            render_members_section(project),
        ],
        size="lg",
        py="xl",
    )


def register(app: Dash) -> None:
    """Wire the project page's create/edit/delete actions, and the sidebar of pinned runs."""

    @app.callback(
        Output(EXP_LIST_ID, "children"),
        Output(NEW_EXP_NAME_ID, "value"),
        Output(NEW_EXP_NAME_ID, "error"),
        Input(NEW_EXP_BUTTON_ID, "n_clicks"),
        Input(NEW_EXP_NAME_ID, "n_submit"),
        State(constants.STATE_PROJECT_ID, "data"),
        State(NEW_EXP_NAME_ID, "value"),
        prevent_initial_call=True,
    )
    def create_experiment(
        n_clicks: int | None, n_submit: int | None, project_id: int, name: str | None
    ) -> tuple[typing.Any, ...]:
        if not n_clicks and not n_submit:
            raise PreventUpdate
        if not name or not name.strip():
            return no_update, no_update, "Give the experiment a name"
        store = get_data_store()
        store.create_experiment(
            NewExperiment(project_id=project_id, name=name.strip(), created_by=get_current_user().id)
        )
        return _experiment_grid(store, project_id), "", None

    def _fetch_project_header(project_id: int) -> tuple[str, str]:
        project = _visible_project(project_id)
        return project.name, project.description

    def _save_project_description(project_id: int, _name: str | None, description: str) -> tuple[str, str]:
        project = _visible_project(project_id)
        updated = get_data_store().update_project(project.model_copy(update={"description": description}))
        return updated.name, updated.description

    register_page_header(
        app,
        PROJECT_HEADER_IDS,
        State(constants.STATE_PROJECT_ID, "data"),
        fetch=_fetch_project_header,
        save=_save_project_description,
    )

    def _delete_project(project_id: int) -> str:
        store = get_data_store()
        store.delete_project(project_id, get_current_user())
        return relative_path("/")

    register_delete_callbacks(
        app,
        PROJECT_DELETE_IDS,
        State(constants.STATE_PROJECT_ID, "data"),
        on_confirm=_delete_project,
    )
    register_members_callbacks(app)
    register_pinned_runs(app)
