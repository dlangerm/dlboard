"""
The basic experiment page plugin: hparam/run table + chart accordion, plus the experiment header.

This module is the plugin's public entrypoint -- the only thing external code (namely
`serve/_pages/experiment.py`, which needs this page's route-skeleton container ids) should import
from. Its own job is just the page-level chrome (header actions, description editing, delete) and
`plug()`, which wires the CSS/JS routes and delegates every feature to its owning submodule:

- `_experiment_page_state.py` -- the page model, its full render tree, and the mutation
  primitives/ids every other submodule below builds on.
- `_panel_controls.py` -- create/rename/delete/move a panel; delete/move a chart; add a chart.
- `_chart_editor_modal.py` -- the add/edit-chart modal's form-building and live preview.
- `_chart_suggestions.py` -- auto-populate charts for an empty view; suggest un-charted keys.
- `_run_comparison_table.py` -- the navbar's hparam/metric/run comparison table + run deletion.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, html
from flask import Response

from dltrack.models import ButtonId, ModalId, constants
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
from dltrack.plugins.pages.experiment import _experiment_page_state as core
from dltrack.plugins.pages.experiment._chart_editor_modal import register_chart_editor_callbacks
from dltrack.plugins.pages.experiment._chart_suggestions import register_chart_suggestions_callbacks
from dltrack.plugins.pages.experiment._panel_controls import register_panel_controls_callbacks
from dltrack.plugins.pages.experiment._run_comparison_table import register_run_comparison_callbacks
from dltrack.serve import get_current_user, get_data_store

if TYPE_CHECKING:
    from dash.development.base_component import Component

# Route-skeleton ids: re-exported from the core module so `serve/_pages/experiment.py` (and
# anything else outside this plugin) imports from this public module, not the private one.
PAGE_EXPERIMENT_ID = core.PAGE_EXPERIMENT_ID
EXPERIMENT_HEADER_ID = core.EXPERIMENT_HEADER_ID
EXPERIMENT_HEADER_ACTIONS_ID = core.EXPERIMENT_HEADER_ACTIONS_ID
METRIC_CONTENT_ID = core.METRIC_CONTENT_ID
STATE_HPARAMS = core.STATE_HPARAMS
STATE_PAGE_STORAGE = core.STATE_PAGE_STORAGE
# Driven directly by browser/e2e tests (dltrack/tests/browser_test.py), so re-exported here too.
NEW_PANEL_ID = core.NEW_PANEL_ID
NEW_PANEL_NAME_ID = core.NEW_PANEL_NAME_ID

BasicExperimentPage = core.BasicExperimentPage

DELETE_EXPERIMENT_BUTTON_ID: ButtonId[core.ExperimentPage] = ButtonId("delete-experiment-button")
DELETE_EXPERIMENT_MODAL_ID: ModalId[core.ExperimentPage] = ModalId("delete-experiment-modal")
DELETE_EXPERIMENT_CONFIRM_ID: ButtonId[core.ExperimentPage] = ButtonId("delete-experiment-confirm")
DELETE_EXPERIMENT_CANCEL_ID: ButtonId[core.ExperimentPage] = ButtonId("delete-experiment-cancel")

EXPERIMENT_DESC_IDS = DescriptionEditorIds(
    header=EXPERIMENT_HEADER_ID,
    edit_button="experiment-edit-desc-button",
    modal="experiment-edit-desc-modal",
    textarea="experiment-edit-desc-textarea",
    save="experiment-edit-desc-save",
    cancel="experiment-edit-desc-cancel",
)

EXPERIMENT_DELETE_IDS = DeleteConfirmIds(
    button=DELETE_EXPERIMENT_BUTTON_ID,
    modal=DELETE_EXPERIMENT_MODAL_ID,
    confirm=DELETE_EXPERIMENT_CONFIRM_ID,
    cancel=DELETE_EXPERIMENT_CANCEL_ID,
)

_HOVER_CSS_PATH = Path(__file__).with_name("_experiment_page_hover.css")
_HOVER_CSS_ROUTE = "experiment-page-hover.css"


def _header_actions() -> Component:
    """
    The header-level actions shown inline with the experiment name.

    Rendered once by `render_initial` alongside the header, not regenerated on later reruns, so
    none of this state is ever at risk of being silently reset by a later panel/chart mutation.

    Everything panel/chart-level -- sync/layout/rename/move/delete for a panel, edit/delete/move
    for a chart, adding a chart -- lives on the panel/chart itself instead, hidden until hovered
    (`dl-panel-controls`/`dl-chart-controls`/`dl-add-chart-btn` in `_experiment_page_hover.css`),
    so none of it requires finding and opening anything first.
    """
    return dmc.Group(
        render_delete_control(EXPERIMENT_DELETE_IDS, label="Delete experiment", entity_noun="experiment"),
        gap="sm",
        wrap="nowrap",
    )


def _serve_hover_css() -> Response:
    return Response(_HOVER_CSS_PATH.read_text(), mimetype="text/css")


def plug(app: Dash) -> None:
    """Plugin for the basic experiment page: hparam table + chart accordion + editor."""
    prefix = str(app.config.routes_pathname_prefix)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    css_route = f"{prefix}{_HOVER_CSS_ROUTE}"
    app.server.add_url_rule(css_route, endpoint=css_route, view_func=_serve_hover_css)
    app.css.append_css({"external_url": css_route, "external_only": True})

    # --- initial render: fills METRIC_CONTENT_ID/EXPERIMENT_HEADER_ID and seeds STATE_PAGE_STORAGE ---
    # Runs into `EXPERIMENT_HEADER_ACTIONS_ID` only once, at page load -- that content (the Runs
    # button and Manage-panels button) is never regenerated by later callbacks.
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(EXPERIMENT_HEADER_ID, "children", allow_duplicate=True),
        Output(EXPERIMENT_HEADER_ACTIONS_ID, "children"),
        Output(STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call="initial_update",
    )
    def render_initial(experiment_id: int) -> tuple[html.Div, Component, Component, str]:
        store = get_data_store()
        page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
        exp = store.get_experiment(experiment_id)
        name, description = (experiment_display_name(exp), exp.description) if exp else ("", "")
        header = render_header(EXPERIMENT_DESC_IDS, title=name, description=description)
        return (
            core.accordion_view(store, experiment_id=experiment_id),
            header,
            _header_actions(),
            page.model_dump_json(),
        )

    def _fetch_experiment_header(experiment_id: int) -> tuple[str, str]:
        store = get_data_store()
        exp = store.get_experiment(experiment_id)
        if exp is None:
            msg = f"Experiment {experiment_id} not found"
            raise ValueError(msg)
        return experiment_display_name(exp), exp.description

    def _save_experiment_description(experiment_id: int, description: str) -> tuple[str, str]:
        store = get_data_store()
        exp = store.get_experiment(experiment_id)
        if exp is None:
            msg = f"Experiment {experiment_id} not found"
            raise ValueError(msg)
        updated = store.update_experiment(exp.model_copy(update={"description": description}))
        return experiment_display_name(updated), updated.description

    register_edit_callbacks(
        app,
        EXPERIMENT_DESC_IDS,
        State(constants.STATE_EXPERIMENT_ID, "data"),
        fetch=_fetch_experiment_header,
        save=_save_experiment_description,
    )

    def _delete_experiment(experiment_id: int) -> str:
        store = get_data_store()
        exp = store.get_experiment(experiment_id)
        if exp is None:
            msg = f"Experiment {experiment_id} not found"
            raise ValueError(msg)
        project_id = exp.project_id
        store.delete_experiment(experiment_id, get_current_user(store))
        return f"/project/{project_id}"

    register_delete_callbacks(
        app,
        EXPERIMENT_DELETE_IDS,
        State(constants.STATE_EXPERIMENT_ID, "data"),
        on_confirm=_delete_experiment,
    )

    core.register_state_callbacks(app)
    register_panel_controls_callbacks(app)
    register_chart_editor_callbacks(app)
    register_chart_suggestions_callbacks(app)
    register_run_comparison_callbacks(app)
