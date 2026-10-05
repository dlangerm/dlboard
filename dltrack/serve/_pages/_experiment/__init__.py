"""
The basic experiment page: hparam/run table + chart accordion, plus the experiment header.

This module's own job is just the page-level chrome (description editing, delete) and `register()`,
which wires the CSS/JS routes and delegates every feature to its owning submodule:

- `_experiment_page_state.py` -- the page model, its full render tree, and the mutation
  primitives/ids every other submodule below (and `serve/_pages/experiment.py`) builds on.
- `_panel_controls.py` -- create/rename/delete/move a panel; delete/move a chart; add a chart.
- `_chart_editor_modal.py` -- the add/edit-chart modal's form-building and live preview.
- `_chart_suggestions.py` -- auto-populate charts for an empty view; suggest un-charted keys.
- `_run_comparison_table.py` -- the navbar's hparam/metric/run comparison table + run deletion.

Anything outside this package that needs a container id, `BasicExperimentPage`, or similar imports
straight from `_experiment_page_state` (the underscore is a "this is an implementation detail you're
reaching into on purpose" signal, not an access restriction) rather than through re-exports here.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pendulum
from dash import ALL, Dash, Input, Output, State, no_update
from structlog.stdlib import get_logger

from dltrack.serve import AssetKind, get_current_user, get_data_store, serve_asset
from dltrack.serve import _constants as constants
from dltrack.serve._component_ids import ButtonId, ModalId
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
from dltrack.serve._pages._experiment._notes import STATE_NOTES_REVISION, register_notes_callbacks
from dltrack.serve._pages._experiment._panel_controls import register_panel_controls_callbacks
from dltrack.serve._pages._experiment._run_comparison_table import register_run_comparison_callbacks
from dltrack.serve._pages._experiment._views import register_view_callbacks
from dltrack.serve._url import relative_path

if TYPE_CHECKING:
    import pandas as pd
    from dash import html
    from dash.development.base_component import Component

    from dltrack import models
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

_ASSETS = [
    (AssetKind.STYLESHEET, Path(__file__).with_name("_experiment_page_hover.css")),
    (AssetKind.SCRIPT, Path(__file__).with_name("_experiment_page_dragdrop.js")),
    (AssetKind.SCRIPT, Path(__file__).with_name("chart_submit_loading.js")),
    (AssetKind.SCRIPT, Path(__file__).with_name("chart_deep_link.js")),
]


def _delete_experiment_action() -> list[Component]:
    """The trash icon shown next to the description's edit icon, plus its confirmation modal."""
    return render_delete_control(
        EXPERIMENT_DELETE_IDS, label="Delete experiment", entity_noun="experiment", icon_only=True
    )


def render_panel(
    store: DataStore[...], ref: core.PageRef, *, focus_chart: str | None = None
) -> tuple[html.Div, Component, str]:
    """
    Build the whole experiment panel: accordion, header, and its persisted page storage.

    Called directly from `serve/_pages/experiment.py`'s `layout()` -- not a callback -- so the
    panel/chart tree exists in the page's very first response instead of being mounted later by a
    follow-up callback. The static shell used to render `METRIC_CONTENT_ID`/`EXPERIMENT_HEADER_ID`
    as empty placeholders and fill them via a `render_initial` callback fired on mount; that meant
    every experiment-page load paid for two full round trips (the static shell, then this content)
    where one now does the job.

    Side effect worth knowing about: `render_initial`'s accordion was always "a component added by
    a later callback" from dash-renderer's perspective, which force-fires any pattern-matching
    `Input` watching it once on mount regardless of `prevent_initial_call` (see
    `_experiment_page_state.py`'s own docstring on this). That forced the accordion's
    `Input({"type": "panel-accordion", ...}, "value")`-watching `persist_open_panel` callback to
    run before any panel ever existed, persisting an explicit `open_panel: []` -- which silently
    defeated `BasicExperimentPage.render()`'s own "open the first panel by default" fallback for
    the rest of that experiment's life, since the fallback only applies when the setting is
    *absent*, not merely empty. The accordion being part of the page's genuine first response now
    means that forced fire no longer happens, so a brand-new experiment's first panel opens by
    default exactly as `render()`'s fallback already says it should.
    """
    page = core.load_page(store, ref)
    exp = store.get_experiment(ref.experiment_id)
    name, description = (experiment_display_name(exp), exp.description) if exp else ("", "")
    header = render_header(
        EXPERIMENT_DESC_IDS,
        title=name,
        description=description,
        extra_actions=_delete_experiment_action(),
    )
    container = core.accordion_view(store, page, focus_chart=focus_chart)
    return container, header, page.model_dump_json()


def _fetch_open_panel_dataframes(
    store: DataStore[...],
    experiment_id: int,
    page: core.BasicExperimentPage,
    panel_names: set[str],
) -> tuple[dict[str, pd.DataFrame], bool]:
    """
    Fetch each of `panel_names`'s own dataframe once -- the same call the initial render makes.

    Not once per chart: "Auto-generate charts" routinely groups several charts sharing a panel, so
    fetching per panel (rather than per chart) keeps a poll tick's cost proportional to how many
    panels are open, not how many charts they hold.
    """
    panels_by_name = {p.name: p for p in page.panels}
    dataframes: dict[str, pd.DataFrame] = {}
    ok = True
    for panel_name in panel_names:
        panel = panels_by_name.get(panel_name)
        if panel is None:
            continue
        try:
            dataframes[panel_name] = core.fetch_panel_dataframe(
                store, experiment_id, panel, page.page_settings
            )
        except Exception:  # noqa: BLE001 -- one bad panel must not break the whole poll tick
            _log.exception("Live-update poll failed to fetch panel %r", panel_name)
            ok = False
    return dataframes, ok


def _refresh_chart_contents(
    chart_ids: list[core.ChartID],
    panels_by_name: dict[str, models.PanelInstance[Any, Any]],
    dataframes: dict[str, pd.DataFrame],
    prev_hashes: dict[str, str] | None,
) -> tuple[list[Any], dict[str, str], bool]:
    """Re-render just the charts whose own `chart_data_fingerprint` moved since the last poll."""
    new_hashes = dict(prev_hashes or {})
    contents: list[Any] = []
    ok = True
    for chart_id in chart_ids:
        panel_name, index = chart_id["panel"], chart_id["index"]
        panel = panels_by_name.get(panel_name)
        df = dataframes.get(panel_name)
        if panel is None or df is None or index is None or index >= len(panel.charts):
            contents.append(no_update)
            continue
        key = f"{panel_name}:{index}"
        try:
            fingerprint = core.chart_data_fingerprint(panel.charts[index], df)
            if fingerprint == new_hashes.get(key):
                contents.append(no_update)
                continue
            new_hashes[key] = fingerprint
            contents.append(core.render_chart_item(panel, index, df))
        except Exception:  # noqa: BLE001 -- one bad chart must not break the whole poll tick
            _log.exception("Live-update poll failed to refresh chart %r", key)
            contents.append(no_update)
            ok = False
    return contents, new_hashes, ok


def register_render_callbacks(app: Dash) -> None:
    """The live-update poll; the initial render itself happens synchronously in `layout()`."""

    # --- live updates: a cheap poll of `Experiment.revision` re-renders only the individual open
    # charts whose own data actually changed (never touching the accordion/tabs shell, closed
    # panels, or sibling charts sharing a panel), so new data appears without any page/panel-level
    # flash a coarser rebuild would cause. Also refreshes `STATE_HPARAMS`, since the navbar
    # run-comparison table (`_run_comparison_table.py`) reads run/hparam data from it, not from a
    # live store query of its own.
    #
    # Each open *panel* is still fetched only once per tick (`fetch_panel_dataframe`, the same call
    # the initial render makes) -- "Auto-generate charts" routinely groups several charts sharing a
    # panel, and fetching once per panel rather than once per chart keeps this poll's per-tick cost
    # proportional to how many panels are open, not how many charts they hold. Each chart's own
    # `chart_data_fingerprint` (scoped to just its own columns of that shared dataframe) is what
    # decides whether *that* chart actually needs to re-render. ---
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
        Output(STATE_NOTES_REVISION, "data"),
        Input(core.LIVE_POLL_INTERVAL_ID, "n_intervals"),
        State({"type": "chart-content", "panel": ALL, "index": ALL}, "id"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(core.STATE_VIEW_ID, "data"),
        State(core.STATE_LAST_KNOWN_REVISION, "data"),
        State(core.STATE_CHART_CONTENT_HASHES, "data"),
        State(STATE_NOTES_REVISION, "data"),
        prevent_initial_call=True,
    )
    def poll_for_updates(  # noqa: PLR0913
        _n_intervals: int,
        chart_ids: list[core.ChartID],
        experiment_id: int,
        view_id: int | None,
        last_known_revision: int | None,
        prev_hashes: dict[str, str] | None,
        last_notes_revision: int | None,
    ) -> tuple[list[Any], Any, Any, Any, Any, Component, Any]:
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
        # Notes changed (posted/deleted) since the last tick: tells an open notes thread to refresh,
        # from the same one-row read -- and independently of whether any chart data changed.
        notes_revision = (
            experiment.notes_revision
            if experiment is not None and experiment.notes_revision != last_notes_revision
            else no_update
        )

        if not ok or experiment is None or experiment.revision == last_known_revision:
            return (
                [no_update] * len(chart_ids),
                no_update,
                no_update,
                no_update,
                fetched_at,
                core.live_status_badge(ok=ok),
                notes_revision,
            )

        page = core.load_page(store, core.PageRef(experiment_id, view_id))
        panels_by_name = {p.name: p for p in page.panels}
        panel_names = {chart_id["panel"] for chart_id in chart_ids}

        dataframes, fetch_ok = _fetch_open_panel_dataframes(store, experiment_id, page, panel_names)
        contents, new_hashes, render_ok = _refresh_chart_contents(
            chart_ids, panels_by_name, dataframes, prev_hashes
        )
        poll_ok = fetch_ok and render_ok

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
            notes_revision,
        )


def register(app: Dash) -> None:
    """Register the experiment page: hparam table + chart accordion + editor."""
    for kind, path in _ASSETS:
        serve_asset(app, kind, path.name, path.read_bytes())

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
        store.delete_experiment(experiment_id, get_current_user())
        return relative_path(f"/project/{project_id}")

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
    register_view_callbacks(app)
    register_notes_callbacks(app)
