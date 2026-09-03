"""
The basic experiment page plugin: hparam/run table + chart accordion, plus the experiment header.

This module's own job is just the page-level chrome (description editing, delete) and `plug()`,
which wires the CSS/JS routes and delegates every feature to its owning submodule:

- `_experiment_page_state.py` -- the page model, its full render tree, and the mutation
  primitives/ids every other submodule below (and `serve/_pages/experiment.py`) builds on.
- `_panel_controls.py` -- create/rename/delete/move a panel; delete/move a chart; add a chart.
- `_chart_editor_modal.py` -- the add/edit-chart modal's form-building and live preview.
- `_chart_suggestions.py` -- auto-populate charts for an empty view; suggest un-charted keys.
- `_run_comparison_table.py` -- the navbar's hparam/metric/run comparison table + run deletion.

Anything outside this plugin that needs a container id, `BasicExperimentPage`, or similar imports
straight from `_experiment_page_state` (the underscore is a "this is an implementation detail you're
reaching into on purpose" signal, not an access restriction) rather than through re-exports here.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pendulum
from dash import ALL, Dash, Input, Output, State, html, no_update
from flask import Response
from structlog.stdlib import get_logger

from dltrack import models
from dltrack.models import ButtonId, ModalId, constants
from dltrack.serve import get_current_user, get_data_store
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
from dltrack.serve._pages._experiment import _experiment_page_state as core
from dltrack.serve._pages._experiment._chart_editor_modal import register_chart_editor_callbacks
from dltrack.serve._pages._experiment._chart_suggestions import register_chart_suggestions_callbacks
from dltrack.serve._pages._experiment._panel_controls import register_panel_controls_callbacks
from dltrack.serve._pages._experiment._run_comparison_table import register_run_comparison_callbacks

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dltrack.models import DataStore

_log = get_logger(__name__)

DELETE_EXPERIMENT_BUTTON_ID: ButtonId[core.ExperimentPage] = ButtonId("delete-experiment-button")
DELETE_EXPERIMENT_MODAL_ID: ModalId[core.ExperimentPage] = ModalId("delete-experiment-modal")
DELETE_EXPERIMENT_CONFIRM_ID: ButtonId[core.ExperimentPage] = ButtonId("delete-experiment-confirm")
DELETE_EXPERIMENT_CANCEL_ID: ButtonId[core.ExperimentPage] = ButtonId("delete-experiment-cancel")

EXPERIMENT_DESC_IDS = DescriptionEditorIds(
    header=core.EXPERIMENT_HEADER_ID,
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
_DRAGDROP_JS_PATH = Path(__file__).with_name("_experiment_page_dragdrop.js")
_DRAGDROP_JS_ROUTE = "experiment-page-dragdrop.js"
_CHART_SUBMIT_LOADING_JS_PATH = Path(__file__).with_name("chart_submit_loading.js")
_CHART_SUBMIT_LOADING_JS_ROUTE = "chart-submit-loading.js"


def _delete_experiment_action() -> list[Component]:
    """The trash icon shown next to the description's edit icon, plus its confirmation modal."""
    return render_delete_control(
        EXPERIMENT_DELETE_IDS, label="Delete experiment", entity_noun="experiment", icon_only=True
    )


def _serve_hover_css() -> Response:
    return Response(_HOVER_CSS_PATH.read_text(), mimetype="text/css")


def _serve_dragdrop_js() -> Response:
    return Response(_DRAGDROP_JS_PATH.read_text(), mimetype="application/javascript")


def _serve_chart_submit_loading_js() -> Response:
    return Response(_CHART_SUBMIT_LOADING_JS_PATH.read_text(), mimetype="application/javascript")


def render_panel(store: DataStore[...], experiment_id: int) -> tuple[html.Div, Component, str]:
    """
    Build the whole experiment panel: accordion, header, and its persisted page storage.

    Called directly from `serve/_pages/experiment.py`'s `layout()` -- not a callback -- so the
    panel/chart tree (with its many `ALL`-pattern-watched buttons and switches) exists in the
    page's very first response instead of being mounted later by a follow-up callback. Dash fires
    an `ALL`-pattern `Input` the instant a matching component is newly mounted, even with
    `prevent_initial_call=True` set on the callback watching it -- that guard only suppresses the
    very first page load, not "this specific component didn't exist yet". A component already
    present in that first response doesn't trigger this at all; one added by a later callback
    always does, regardless of which callback or how many guards it has. Every one of those
    otherwise-inevitable, individually-cheap-but-still-real round trips (panel-sync switches,
    layout controls, delete/rename buttons, ...) is the difference between this page loading in
    around half a second and over a second and a half -- measured, not assumed.
    """
    page = store.get_or_create_page(core.BasicExperimentPage, experiment_id=experiment_id)
    exp = store.get_experiment(experiment_id)
    name, description = (experiment_display_name(exp), exp.description) if exp else ("", "")
    header = render_header(
        EXPERIMENT_DESC_IDS,
        title=name,
        description=description,
        extra_actions=_delete_experiment_action(),
    )
    container = core.accordion_view(store, experiment_id=experiment_id)
    return container, header, page.model_dump_json()


def _needs_rerender(experiment: models.Experiment | None, last_known_revision: int | None) -> bool:
    """Whether a poll tick found a real change: the experiment still exists and its revision moved."""
    return experiment is not None and experiment.revision != last_known_revision


def register_render_callbacks(app: Dash) -> None:
    """The live-update poll; the initial render itself now happens synchronously in `layout()`."""

    # --- live updates: cheap poll of Experiment.revision, patching only the individual charts whose
    # own data actually changed (never touching the accordion/tabs shell, closed panels, or sibling
    # charts sharing a panel) so new data appears without any page/panel-level flash a coarser
    # rebuild would cause. Also refreshes STATE_HPARAMS, since the navbar run-comparison table
    # (`_run_comparison_table.py`) reads run/hparam data from it, not from a live store query.
    #
    # Granularity is per *chart*, not per *panel*: `PanelInstance.hint_required_columns()` unions
    # every chart's columns, so `fetch_panel_dataframe` fetches one shared dataframe for a whole
    # panel -- fine for the initial render, but "Auto-generate charts" routinely groups an
    # epoch-level chart next to a per-step one in the same panel, and a panel-level fingerprint
    # would pop the untouched chart every time its sibling's data moved. Each chart is instead
    # re-fetched through a synthetic single-chart `PanelInstance` (reusing `fetch_panel_dataframe`
    # unchanged, just scoped to one chart's own required columns), so its own fingerprint reflects
    # only what that chart actually shows. ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        # `chart_content_id(...)` is typed `(str, int) -> ChartID` (every other call site passes a
        # real panel name/index), so it can't be called with the `ALL` wildcard without breaking
        # that typing -- same reason `_panel_controls.py`'s own `ALL`-pattern callbacks spell their
        # pattern-matching ids out as literal dicts too, rather than routing through a helper.
        Output({"type": "chart-content", "panel": ALL, "index": ALL}, "children"),
        Output(core.STATE_HPARAMS, "data", allow_duplicate=True),
        Output(core.STATE_LAST_KNOWN_REVISION, "data", allow_duplicate=True),
        Output(core.STATE_CHART_CONTENT_HASHES, "data", allow_duplicate=True),
        Output(core.STATE_LAST_FETCH_AT, "data", allow_duplicate=True),
        Output(core.LIVE_STATUS_ID, "children"),
        Input(core.LIVE_POLL_INTERVAL_ID, "n_intervals"),
        State({"type": "chart-content", "panel": ALL, "index": ALL}, "id"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(core.STATE_LAST_KNOWN_REVISION, "data"),
        State(core.STATE_CHART_CONTENT_HASHES, "data"),
        prevent_initial_call=True,
    )
    def poll_for_updates(
        _n_intervals: int,
        chart_ids: list[core.ChartID],
        experiment_id: int,
        last_known_revision: int | None,
        prev_hashes: dict[str, str] | None,
    ) -> tuple[list[Any], Any, Any, Any, Any, Component]:
        store = get_data_store()
        ok = True
        try:
            experiment = store.get_experiment(experiment_id)
        except Exception:  # noqa: BLE001 -- a poll failure is reported via the status badge, not raised
            _log.exception("Live-update poll failed for experiment %s", experiment_id)
            experiment, ok = None, False

        # Set on every tick that actually reached the store, whether or not anything had changed --
        # "how stale could this be" (`LIVE_LAST_FETCH_LABEL_ID`, ticked purely client-side) needs the
        # time of the last real check, not the time of the last real change.
        fetched_at = pendulum.now("UTC").isoformat() if ok else no_update

        if not ok or experiment is None or not _needs_rerender(experiment, last_known_revision):
            return (
                [no_update] * len(chart_ids),
                no_update,
                no_update,
                no_update,
                fetched_at,
                core.live_status_badge(ok=ok),
            )

        page = cast(
            "core.BasicExperimentPage",
            store.get_or_create_page(core.BasicExperimentPage, experiment_id=experiment_id),
        )
        panels_by_name = {p.name: p for p in page.panels}

        new_hashes = dict(prev_hashes or {})
        contents: list[Any] = []
        poll_ok = True
        for chart_id in chart_ids:
            panel_name, index = chart_id["panel"], chart_id["index"]
            panel = panels_by_name.get(panel_name)
            if panel is None or index is None or index >= len(panel.charts):
                contents.append(no_update)
                continue
            key = f"{panel_name}:{index}"
            try:
                chart = panel.charts[index]
                single_chart_panel = models.PanelInstance(name=panel.name, charts=[chart])
                df = core.fetch_panel_dataframe(store, experiment_id, single_chart_panel, page.page_settings)
                fingerprint = core.panel_data_fingerprint(df)
                if fingerprint == new_hashes.get(key):
                    contents.append(no_update)
                    continue
                new_hashes[key] = fingerprint
                contents.append(core.render_chart_item(panel, index, df))
            except Exception:  # noqa: BLE001 -- one bad chart must not break the whole poll tick
                _log.exception("Live-update poll failed to refresh chart %r", key)
                contents.append(no_update)
                poll_ok = False

        try:
            hparams_json: Any = [h.model_dump_json() for h in store.fetch_hyperparams(experiment_id)]
        except Exception:  # noqa: BLE001 -- charts refreshed above still ship even if this fails
            _log.exception(
                "Live-update poll failed to refresh hyperparameters for experiment %s", experiment_id
            )
            hparams_json, poll_ok = no_update, False

        return (
            contents,
            hparams_json,
            experiment.revision,
            new_hashes,
            fetched_at,
            core.live_status_badge(ok=poll_ok),
        )


def register(app: Dash) -> None:
    """Register the experiment page: hparam table + chart accordion + editor."""
    prefix = str(app.config.routes_pathname_prefix)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    css_route = f"{prefix}{_HOVER_CSS_ROUTE}"
    app.server.add_url_rule(css_route, endpoint=css_route, view_func=_serve_hover_css)
    app.css.append_css({"external_url": css_route, "external_only": True})

    dragdrop_route = f"{prefix}{_DRAGDROP_JS_ROUTE}"
    app.server.add_url_rule(dragdrop_route, endpoint=dragdrop_route, view_func=_serve_dragdrop_js)
    app.scripts.append_script(  # pyright: ignore[reportUnknownMemberType]
        {"external_url": dragdrop_route, "external_only": True}
    )

    chart_submit_loading_route = f"{prefix}{_CHART_SUBMIT_LOADING_JS_ROUTE}"
    app.server.add_url_rule(
        chart_submit_loading_route,
        endpoint=chart_submit_loading_route,
        view_func=_serve_chart_submit_loading_js,
    )
    app.scripts.append_script(  # pyright: ignore[reportUnknownMemberType]
        {"external_url": chart_submit_loading_route, "external_only": True}
    )

    register_render_callbacks(app)

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
