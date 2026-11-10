"""
The experiment page's header: its name and description, deleting it, and the page's own controls beside them.

What an experiment's header shows is drawn here, once, from the experiment itself: a new item is one more entry below.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import dash_mantine_components as dmc
from dash import State

from dlboard.serve import _constants as constants
from dlboard.serve import get_current_user, get_data_store
from dlboard.serve._component_ids import ButtonId, ModalId
from dlboard.serve._pages._dataframe_helpers import experiment_display_name
from dlboard.serve._pages._delete_confirm import (
    DeleteConfirmIds,
    register_delete_callbacks,
    render_delete_control,
)
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment._live_updates import live_controls
from dlboard.serve._pages._experiment._notes import notes_button
from dlboard.serve._pages._experiment._views import view_controls
from dlboard.serve._pages._page_header import PageHeaderIds, page_header, register_page_header
from dlboard.serve._pages._page_sections import ExperimentSlot, experiment_sections
from dlboard.serve._url import relative_path

if TYPE_CHECKING:
    from dash import Dash
    from dash.development.base_component import Component

    from dlboard.models import DataStore, Experiment
    from dlboard.serve._pages._experiment._page_model import BasicExperimentPage

EXPERIMENT_HEADER_IDS = PageHeaderIds[ids.ExperimentPage].of("experiment")

EXPERIMENT_DELETE_IDS = DeleteConfirmIds(
    button=ButtonId[ids.ExperimentPage]("delete-experiment-button"),
    modal=ModalId[ids.ExperimentPage]("delete-experiment-modal"),
    confirm=ButtonId[ids.ExperimentPage]("delete-experiment-confirm"),
    cancel=ButtonId[ids.ExperimentPage]("delete-experiment-cancel"),
)


def experiment_header(
    store: DataStore[...], experiment: Experiment, page: BasicExperimentPage, view_id: int | None
) -> Component:
    """The header of `experiment`, showing `page` (its view `view_id`, or the shared page)."""
    return page_header(
        EXPERIMENT_HEADER_IDS,
        title=experiment_display_name(experiment),
        description=experiment.description,
        actions=render_delete_control(
            EXPERIMENT_DELETE_IDS, label="Delete experiment", entity_noun="experiment", icon_only=True
        ),
        meta=experiment_sections(ExperimentSlot.HEADER, experiment),
        aside=[
            dmc.Group(
                [
                    notes_button(len(store.list_comments(experiment.id))),
                    view_controls(store, experiment.id, page, view_id),
                    dmc.Divider(orientation="vertical"),
                    *live_controls(),
                ],
                gap="xs",
                wrap="nowrap",
                style={"alignSelf": "center"},
            )
        ],
    )


def _found(experiment_id: int) -> Experiment:
    experiment = get_data_store().get_experiment(experiment_id)
    if experiment is None:
        msg = f"Experiment {experiment_id} not found"
        raise ValueError(msg)
    return experiment


def register_header_callbacks(app: Dash) -> None:
    """Edit the experiment's description, and delete it."""

    def fetch(experiment_id: int) -> tuple[str, str]:
        experiment = _found(experiment_id)
        return experiment_display_name(experiment), experiment.description

    def save(experiment_id: int, _name: str | None, description: str) -> tuple[str, str]:
        updated = get_data_store().update_experiment(
            _found(experiment_id).model_copy(update={"description": description})
        )
        return experiment_display_name(updated), updated.description

    def delete(experiment_id: int) -> str:
        project_id = _found(experiment_id).project_id
        get_data_store().delete_experiment(experiment_id, get_current_user())
        return relative_path(f"/project/{project_id}")

    experiment = State(constants.STATE_EXPERIMENT_ID, "data")
    register_page_header(app, EXPERIMENT_HEADER_IDS, experiment, fetch=fetch, save=save)
    register_delete_callbacks(app, EXPERIMENT_DELETE_IDS, experiment, on_confirm=delete)
