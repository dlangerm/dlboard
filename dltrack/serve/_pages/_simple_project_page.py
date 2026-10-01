"""A project page: its experiments as cards, plus an inline "new experiment" field."""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING

import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, html, no_update

from dltrack.models import ActivityStats, ButtonId, ModalId, NewExperiment, constants
from dltrack.serve import Icon, get_current_user, get_data_store, icon
from dltrack.serve._pages._dash_helpers import entity_card
from dltrack.serve._pages._dataframe_helpers import experiment_display_name
from dltrack.serve._pages._delete_confirm import (
    DeleteConfirmIds,
    register_delete_callbacks,
    render_delete_control,
)
from dltrack.serve._pages._description_editor import (
    DescriptionEditorIds,
    register_edit_callbacks,
    render_header,
)
from dltrack.serve._pages._onboarding import first_experiment_snippet, onboarding_card
from dltrack.serve._pages._project_members import register_members_callbacks, render_members_section

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dltrack.models import DataStore, Experiment, Project


class _ProjectPage:
    """Page tag: marks a component id as belonging to `simple_project_page.py`."""


PROJECT_HEADER_ID: typing.Final = "project-header"
EXP_LIST_ID: typing.Final = "experiment-list-id"
NEW_EXP_BUTTON_ID: typing.Final = "new-experiment-button"
NEW_EXP_NAME_ID: typing.Final = "new-experiment-name"

DELETE_PROJECT_BUTTON_ID: ButtonId[_ProjectPage] = ButtonId("delete-project-button")
DELETE_PROJECT_MODAL_ID: ModalId[_ProjectPage] = ModalId("delete-project-modal")
DELETE_PROJECT_CONFIRM_ID: ButtonId[_ProjectPage] = ButtonId("delete-project-confirm")
DELETE_PROJECT_CANCEL_ID: ButtonId[_ProjectPage] = ButtonId("delete-project-cancel")

PROJECT_DESC_IDS = DescriptionEditorIds(
    header=PROJECT_HEADER_ID,
    edit_button="project-edit-desc-button",
    modal="project-edit-desc-modal",
    textarea="project-edit-desc-textarea",
    save="project-edit-desc-save",
    cancel="project-edit-desc-cancel",
)

PROJECT_DELETE_IDS = DeleteConfirmIds(
    button=DELETE_PROJECT_BUTTON_ID,
    modal=DELETE_PROJECT_MODAL_ID,
    confirm=DELETE_PROJECT_CONFIRM_ID,
    cancel=DELETE_PROJECT_CANCEL_ID,
)


def _delete_project_action() -> list[Component]:
    """The trash icon shown next to the header's edit icon, plus its confirmation modal."""
    return render_delete_control(
        PROJECT_DELETE_IDS, label="Delete project", entity_noun="project", icon_only=True
    )


def _experiment_card(experiment: Experiment, stats: ActivityStats) -> Component:
    return entity_card(
        title=experiment_display_name(experiment),
        description=experiment.description,
        href=f"/experiment/{experiment.id}",
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
            dmc.Group(
                [
                    html.Div(
                        id=PROJECT_HEADER_ID,
                        children=render_header(
                            PROJECT_DESC_IDS,
                            title=project.name,
                            description=project.description,
                            extra_actions=_delete_project_action(),
                        ),
                        style={"flex": 1, "minWidth": 0},
                    ),
                    dmc.Group(
                        [
                            dmc.TextInput(
                                id=NEW_EXP_NAME_ID,
                                placeholder="New experiment name",
                                w=240,
                                **typing.cast("dict[str, typing.Any]", {"aria-label": "New experiment name"}),
                            ),
                            dmc.Button("Create", id=NEW_EXP_BUTTON_ID, leftSection=icon(Icon.ADD)),
                        ],
                        gap="xs",
                        align="flex-start",
                        wrap="nowrap",
                    ),
                ],
                justify="space-between",
                align="flex-start",
                mb="lg",
            ),
            dmc.Box(_experiment_grid(store, project_id), id=EXP_LIST_ID),
            render_members_section(project),
        ],
        size="lg",
        py="xl",
    )


def register(app: Dash) -> None:
    """Wire the project page's create/edit/delete actions."""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
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
        _clicks: int, _submits: int, project_id: int, name: str | None
    ) -> tuple[typing.Any, ...]:
        if not name or not name.strip():
            return no_update, no_update, "Give the experiment a name"
        store = get_data_store()
        store.create_experiment(
            NewExperiment(project_id=int(project_id), name=name.strip(), created_by=get_current_user().id)
        )
        return _experiment_grid(store, int(project_id)), "", None

    def _fetch_project_header(project_id: int) -> tuple[str, str]:
        project = _visible_project(project_id)
        return project.name, project.description

    def _save_project_description(project_id: int, description: str) -> tuple[str, str]:
        project = _visible_project(project_id)
        updated = get_data_store().update_project(project.model_copy(update={"description": description}))
        return updated.name, updated.description

    register_edit_callbacks(
        app,
        PROJECT_DESC_IDS,
        State(constants.STATE_PROJECT_ID, "data"),
        fetch=_fetch_project_header,
        save=_save_project_description,
        extra_actions=_delete_project_action(),
    )

    def _delete_project(project_id: int) -> str:
        store = get_data_store()
        store.delete_project(project_id, get_current_user())
        return "/"

    register_delete_callbacks(
        app,
        PROJECT_DELETE_IDS,
        State(constants.STATE_PROJECT_ID, "data"),
        on_confirm=_delete_project,
    )
    register_members_callbacks(app)
