"""
The basic experiment page: hparam/run table + chart accordion, plus the experiment header.

This module's own job is just the page-level chrome (description editing, delete) and `register()`,
which wires the CSS/JS routes and delegates everything else to its submodules.

Layers, each importing only from the ones above it:

- `_page_model.py` -- the persisted page and every pure edit to its panels (no Dash, no store).
- `_page_repository.py` -- loading and saving a page, branching into a view of your own on edit.
- `_panel_data.py` -- fetching a panel's data, and the fingerprint the live poll compares.
- `_ids.py` -- the page tag and the few ids more than one feature reads or writes.
- `_chart_view.py`, `_panel_view.py` -- the panel area's render tree, handed its data (never a store).
- `_page_edits.py` -- "edit, save, re-render", the step every feature's edit callback ends with.

Features, each owning its ids, its modal or drawer, and its callbacks:

- `_panel_controls.py` -- create/rename/delete/move a panel; delete/move a chart.
- `_tab_controls.py` -- create/rename a tab, drop a panel or chart onto one.
- `_chart_editor.py` -- the chart editor docked beside the panel area: a chart is added or edited with the page in view.
- `_chart_settings_form.py` -- the form of a chart type's settings, and reading its values back.
- `_chart_suggestions.py` -- auto-populate charts for an empty view; suggest un-charted keys.
- `_run_comparison_table.py` -- the navbar's hparam/metric/run comparison table + run deletion.
- `_panel_area_controls.py` -- paging and filtering the panel area, opening panels, switching tabs.
- `_live_updates.py` -- the poll that redraws only charts whose data changed, and its status.

Anything outside this package imports straight from the submodule that owns what it needs (the
underscore is a "this is an implementation detail you're reaching into on purpose" signal, not an
access restriction) rather than through re-exports here.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from dash import Dash, State
from structlog.stdlib import get_logger

from dlboard.serve import AssetKind, get_current_user, get_data_store, serve_asset
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import ButtonId, ModalId
from dlboard.serve._pages._dataframe_helpers import experiment_display_name
from dlboard.serve._pages._delete_confirm import (
    DeleteConfirmIds,
    register_delete_callbacks,
    render_delete_control,
)
from dlboard.serve._pages._description_editor import (
    DescriptionEditorIds,
    register_edit_callbacks,
    render_header,
)
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment import _panel_data as panel_data
from dlboard.serve._pages._experiment import _panel_view as panel_view
from dlboard.serve._pages._experiment._chart_editor import register_chart_editor_callbacks
from dlboard.serve._pages._experiment._chart_suggestions import register_chart_suggestions_callbacks
from dlboard.serve._pages._experiment._live_updates import register_live_update_callbacks
from dlboard.serve._pages._experiment._notes import register_notes_callbacks
from dlboard.serve._pages._experiment._paging import visible_open_panels
from dlboard.serve._pages._experiment._panel_area_controls import register_panel_area_callbacks
from dlboard.serve._pages._experiment._panel_controls import register_panel_controls_callbacks
from dlboard.serve._pages._experiment._run_compare import register_run_compare_callbacks
from dlboard.serve._pages._experiment._run_comparison_table import register_run_comparison_callbacks
from dlboard.serve._pages._experiment._tab_controls import register_tab_controls_callbacks
from dlboard.serve._pages._experiment._views import register_view_callbacks
from dlboard.serve._url import relative_path

if TYPE_CHECKING:
    from dash import html
    from dash.development.base_component import Component

    from dlboard.models import DataStore
    from dlboard.serve._pages._experiment._paging import Paging

_log = get_logger(__name__)

DELETE_EXPERIMENT_BUTTON_ID: ButtonId[ids.ExperimentPage] = ButtonId("delete-experiment-button")
DELETE_EXPERIMENT_MODAL_ID: ModalId[ids.ExperimentPage] = ModalId("delete-experiment-modal")
DELETE_EXPERIMENT_CONFIRM_ID: ButtonId[ids.ExperimentPage] = ButtonId("delete-experiment-confirm")
DELETE_EXPERIMENT_CANCEL_ID: ButtonId[ids.ExperimentPage] = ButtonId("delete-experiment-cancel")

EXPERIMENT_DESC_IDS = DescriptionEditorIds(
    header=ids.EXPERIMENT_HEADER_ID,
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

_ASSETS = [
    (AssetKind.STYLESHEET, Path(__file__).with_name("_experiment_page_hover.css")),
    (AssetKind.SCRIPT, Path(__file__).with_name("_experiment_page_dragdrop.js")),
    (AssetKind.SCRIPT, Path(__file__).with_name("chart_deep_link.js")),
]


def _delete_experiment_action() -> list[Component]:
    """The trash icon shown next to the description's edit icon, plus its confirmation modal."""
    return render_delete_control(
        EXPERIMENT_DELETE_IDS, label="Delete experiment", entity_noun="experiment", icon_only=True
    )


def render_panel(
    store: DataStore[...], ref: repo.PageRef, paging: Paging, *, focus_chart: str | None = None
) -> tuple[html.Div, Component, str]:
    """
    Build the whole experiment panel: accordion, header, and its persisted page storage.

    Called directly from `serve/_pages/experiment.py`'s `layout()` -- not a callback -- so the
    panel/chart tree exists in the page's very first response instead of being mounted later by a
    follow-up callback. The static shell used to render `METRIC_CONTENT_ID`/`EXPERIMENT_HEADER_ID`
    as empty placeholders and fill them via a `render_initial` callback fired on mount; that meant
    every experiment-page load paid for two full round trips (the static shell, then this content)
    where one now does the job.
    """
    page = repo.load_page(store, ref)
    exp = store.get_experiment(ref.experiment_id)
    name, description = (experiment_display_name(exp), exp.description) if exp else ("", "")
    header = render_header(
        EXPERIMENT_DESC_IDS,
        title=name,
        description=description,
        extra_actions=_delete_experiment_action(),
    )
    paging = panel_view.resolved_paging(page, paging, focus_chart=focus_chart)
    open_panels = visible_open_panels(page.panels, paging.shown_tab(page.panels), paging)
    container = panel_view.accordion_view(
        panel_data.panel_frames(store, page, paging, open_panels), page, paging
    )
    return container, header, page.model_dump_json()


def register(app: Dash) -> None:
    """Register the experiment page: hparam table + chart accordion + editor."""
    for kind, path in _ASSETS:
        serve_asset(app, kind, path.name, path.read_bytes())

    register_live_update_callbacks(app)

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
        extra_actions=_delete_experiment_action(),
    )

    def _delete_experiment(experiment_id: int) -> str:
        store = get_data_store()
        exp = store.get_experiment(experiment_id)
        if exp is None:
            msg = f"Experiment {experiment_id} not found"
            raise ValueError(msg)
        project_id = exp.project_id
        store.delete_experiment(experiment_id, get_current_user())
        return relative_path(f"/project/{project_id}")

    register_delete_callbacks(
        app,
        EXPERIMENT_DELETE_IDS,
        State(constants.STATE_EXPERIMENT_ID, "data"),
        on_confirm=_delete_experiment,
    )

    register_panel_area_callbacks(app)
    register_panel_controls_callbacks(app)
    register_tab_controls_callbacks(app)
    register_chart_editor_callbacks(app)
    register_chart_suggestions_callbacks(app)
    register_run_comparison_callbacks(app)
    register_run_compare_callbacks(app)
    register_view_callbacks(app)
    register_notes_callbacks(app)
