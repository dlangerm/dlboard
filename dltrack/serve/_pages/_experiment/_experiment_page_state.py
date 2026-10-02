"""
Core state/render layer for the basic experiment page.

Owns the page's persisted model (`BasicExperimentPage`), its full static render tree
(`accordion_view` -- the accordion itself plus every modal/drawer shell reachable from it), the
mutation primitives every controller module below builds on, and every component id shared across
more than one of those controller modules. Nothing in this module imports from
`_panel_controls.py`, `_chart_editor_modal.py`, `_chart_suggestions.py`, or
`_run_comparison_table.py` -- they all import from here instead, which is what keeps the split
acyclic: this is the one module every other piece of the experiment page depends on.
"""

from __future__ import annotations

import hashlib
import itertools
import typing
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

import dash_mantine_components as dmc
import pandas as pd
from dash import ALL, Dash, Input, Output, State, ctx, html, no_update
from dash.dcc import Store
from dash.development.base_component import Component
from dash.exceptions import PreventUpdate
from pydantic_settings import BaseSettings
from structlog.stdlib import get_logger

from dltrack import models
from dltrack.models import RUN_NAME_COLUMN, ButtonId, DivId, IntervalId, ModalId, StoreId, ValueId, constants
from dltrack.serve import ClientsideScript, Icon, get_current_user, get_data_store, icon
from dltrack.serve._pages._dash_helpers import tooltipped_action_icon
from dltrack.serve._pages._dataframe_helpers import run_display_name
from dltrack.serve._pages._experiment import _dataframe_helpers as dfh

if TYPE_CHECKING:
    from collections.abc import Callable

    from dltrack.models import DataStore

_log = get_logger(__name__)

_ACTIVE_TAB_DISABLES_RENAME_JS = ClientsideScript(Path(__file__).with_name("active_tab_disables_rename.js"))
_LIVE_SWITCH_STATE_JS = ClientsideScript(Path(__file__).with_name("live_switch_state.js"))
_LIVE_LAST_FETCH_LABEL_JS = ClientsideScript(Path(__file__).with_name("live_last_fetch_label.js"))


class ExperimentPage:
    """Page tag: marks a component id as belonging to the experiment page."""


# --- route-skeleton ids: a two-file contract with `serve/_pages/experiment.py`, which imports
# these from `_experiment/__init__.py` (this package's public façade) rather than from here. ---
PAGE_EXPERIMENT_ID: DivId[ExperimentPage] = DivId("experiment-container")
EXPERIMENT_HEADER_ID: DivId[ExperimentPage] = DivId("experiment-header")
METRIC_CONTENT_ID: DivId[ExperimentPage] = DivId("metrics-view")
STATE_HPARAMS: StoreId[ExperimentPage] = StoreId("hparams-state")
STATE_PAGE_STORAGE: StoreId[ExperimentPage] = StoreId("current-page")
STATE_VIEW_ID: StoreId[ExperimentPage] = StoreId("view-id-state")
"""The named view (`Page.id`) this page is showing, from its `?view=` URL, or `None` for the shared page."""

# --- live updates: a `dcc.Interval`-driven poll of `Experiment.revision` re-renders whichever open
# charts have actually changed since the last poll; a small badge reports whether the poll itself
# is healthy. See `poll_for_updates` in `__init__.py`. ---
LIVE_POLL_INTERVAL_ID: IntervalId[ExperimentPage] = IntervalId("live-poll-interval")
STATE_LAST_KNOWN_REVISION: StoreId[ExperimentPage] = StoreId("last-known-revision")
STATE_CHART_CONTENT_HASHES: StoreId[ExperimentPage] = StoreId("chart-content-hashes")
"""`dict["panel:index", chart_data_fingerprint(...)]` for whichever charts a poll tick last checked
-- keyed per chart, not per panel, since two charts sharing a panel (routinely, via "Auto-generate
charts" grouping related metrics together) commonly have very different update cadences."""
LIVE_STATUS_ID: DivId[ExperimentPage] = DivId("live-status")

# --- enable/disable + "fetched Xs ago": both pure client-side concerns, deliberately kept off the
# server. `LIVE_UPDATES_ENABLED_ID`'s `checked` persists to the browser's own `localStorage` (Dash's
# built-in `persistence`/`persistence_type="local"`, set where it's rendered in
# `serve/_pages/experiment.py`) -- a per-browser viewing preference, not something to write into
# `Experiment`/`Page` and have every other viewer inherit. `LIVE_TICK_INTERVAL_ID` ticks
# `LIVE_LAST_FETCH_LABEL_ID`'s text once a second purely from `STATE_LAST_FETCH_AT` (a clientside
# callback, see `live_last_fetch_label.js`) -- no server round trip just to update a clock. ---
LIVE_UPDATES_ENABLED_ID: ValueId[ExperimentPage] = ValueId("live-updates-enabled")
LIVE_TICK_INTERVAL_ID: IntervalId[ExperimentPage] = IntervalId("live-tick-interval")
LIVE_LAST_FETCH_LABEL_ID: DivId[ExperimentPage] = DivId("live-last-fetch-label")
LIVE_PAUSED_BADGE_ID: DivId[ExperimentPage] = DivId("live-paused-badge")
"""The "Paused" badge shown instead of `LIVE_STATUS_ID` while live updates are switched off -- a
sibling element toggled by CSS `display` (see `live_switch_state.js`), not a second callback
writing `LIVE_STATUS_ID.children` -- `poll_for_updates` stays that output's only owner."""
STATE_LAST_FETCH_AT: StoreId[ExperimentPage] = StoreId("last-fetch-at")
"""ISO 8601 UTC timestamp of the last poll tick that actually reached the store successfully --
set on *every* such tick, whether or not anything had changed, since it answers "how stale could
what I'm looking at be" rather than "when did it last change"."""


class LiveUpdateSettings(BaseSettings):
    """Environment variables controlling the experiment page's live-update poll."""

    poll_interval_ms: int = 30000
    """
    How often the browser checks whether an open experiment has changed. Each check is one indexed
    primary-key read, cheap even with many concurrent viewers -- turn this down (or up) for a
    heavily-loaded multi-user deployment without a code change.
    """

    tick_interval_ms: int = 10000
    """How often the "fetched Xs ago" label re-renders. Purely client-side (see
    `LIVE_TICK_INTERVAL_ID`) -- this never touches the server, so it's fine to leave far more
    frequent than `poll_interval_ms`."""


# --- drag-and-drop reorder requests: a completed drag reports here (`_experiment_page_dragdrop.js`
# calls `set_props` on drop), consumed by a callback in `_panel_controls.py`. Live in the static
# page layout (`serve/_pages/experiment.py`), not `accordion_view`'s render tree, so the drop
# target always exists regardless of what's currently rendered underneath it. ---
PANEL_REORDER_STORE_ID: StoreId[ExperimentPage] = StoreId("panel-reorder-request")
CHART_REORDER_STORE_ID: StoreId[ExperimentPage] = StoreId("chart-reorder-request")
TAB_DROP_STORE_ID: StoreId[ExperimentPage] = StoreId("tab-drop-request")
CHART_TAB_DROP_STORE_ID: StoreId[ExperimentPage] = StoreId("chart-tab-drop-request")
CHART_PANEL_MOVE_STORE_ID: StoreId[ExperimentPage] = StoreId("chart-panel-move-request")

# --- accordion / panel state ---
# One `Accordion` per tab group, id'd via `panel_accordion_id` -- see `panel_accordion_id`/`BasicExperimentPage.render`.
OPEN_PANEL_KEY: typing.Final = "open_panel"  # a page_settings dict key, not a component id
ACTIVE_TAB_KEY: typing.Final = "active_tab"  # a page_settings dict key, not a component id
PANEL_TABS_ID: ValueId[ExperimentPage] = ValueId("experiment-panel-tabs")
_UNGROUPED_TAB: typing.Final = ""
"""`PanelInstance.tab`'s default -- panels with no explicit tab all share this one implicit group."""

_UNGROUPED_TAB_VALUE: typing.Final = "ungrouped"
"""
`dmc.Tabs`/`TabsTab`/`TabsPanel`'s own `value` prop, for `_UNGROUPED_TAB` specifically.

Mantine's `Tabs` treats an empty-string `value` as "no value was set" and refuses to render that
tab/panel at all (a real, silent failure: the tab becomes unclickable, so there's no way back to
the ungrouped panels once another tab exists) -- so component `value`s always go through
`tab_component_value`/`tab_from_component_value` instead of using `PanelInstance.tab`/
`ACTIVE_TAB_KEY`'s own `""` directly. A Private-Use-Area character prefix keeps this from
colliding with a tab name any user could actually type into the new-tab modal. Not underscored --
`_panel_controls.py`'s per-panel tab-move dropdown needs the same mapping for its own `Select`.
"""


def tab_component_value(tab: str) -> str:
    """Map a real tab name (`""` for the ungrouped tab) to what a Mantine component accepts as `value`."""
    return tab or _UNGROUPED_TAB_VALUE


def tab_from_component_value(value: str) -> str:
    """Invert `tab_component_value` -- what a component's `value`/`Input` reports back."""
    return "" if value == _UNGROUPED_TAB_VALUE else value


LOADED_PANELS_STORE_ID: StoreId[ExperimentPage] = StoreId("loaded-panels-store")

# --- new-panel popover, anchored to the toolbar's "New panel" button (callback in `_panel_controls.py`) ---
NEW_PANEL_OPEN_ID: ButtonId[ExperimentPage] = ButtonId("new-panel-open")
NEW_PANEL_NAME_ID: ValueId[ExperimentPage] = ValueId("panel-name")
NEW_PANEL_ID: ButtonId[ExperimentPage] = ButtonId("new-panel-button")

# --- add/edit-chart modal shell (content is filled in by `_chart_editor_modal.py`'s callbacks) ---
ADD_CHART_MODAL_ID: ModalId[ExperimentPage] = ModalId("add-chart-modal")
ADD_CHART_TARGET_ID: StoreId[ExperimentPage] = StoreId("add-chart-target")
ADD_CHART_INITIAL_PARAMS_ID: StoreId[ExperimentPage] = StoreId("add-chart-initial-params")
ADD_CHART_TYPE_SELECT_ID: ValueId[ExperimentPage] = ValueId("add-chart-type-select")
ADD_CHART_PARAMS_ID: DivId[ExperimentPage] = DivId("add-chart-params")
ADD_CHART_PREVIEW_ID: DivId[ExperimentPage] = DivId("add-chart-preview")
ADD_CHART_ERROR_ID: DivId[ExperimentPage] = DivId("add-chart-error")
ADD_CHART_SUBMIT_ID: ButtonId[ExperimentPage] = ButtonId("add-chart-submit")
ADD_CHART_CANCEL_ID: ButtonId[ExperimentPage] = ButtonId("add-chart-cancel")

# A chart-parameter field's own id is a pattern-matched dict, not a scalar id -- shared between
# `_chart_editor_modal.py` (builds the form / clears a field) and `_panel_controls.py` (reads the
# submitted form back out in `add_chart`), so the "chart-param" type string lives here once.
CHART_PARAM_TYPE: typing.Final = "chart-param"
_CHART_PARAM_CLEAR_TYPE: typing.Final = "chart-param-clear"


def chart_param_id(field_name: str) -> dict[str, str]:
    return {"type": CHART_PARAM_TYPE, "field": field_name}


def chart_param_clear_id(field_name: str) -> dict[str, str]:
    return {"type": _CHART_PARAM_CLEAR_TYPE, "field": field_name}


# --- suggest-charts drawer shell (content is filled in by `_chart_suggestions.py`'s callbacks) ---
SUGGEST_CHARTS_BUTTON_ID: ButtonId[ExperimentPage] = ButtonId("suggest-charts-button")
SUGGEST_DRAWER_ID: ModalId[ExperimentPage] = ModalId("suggest-charts-drawer")
SUGGEST_DELIMITER_ID: ValueId[ExperimentPage] = ValueId("suggest-charts-delimiter")
SUGGEST_MODE_ID: ValueId[ExperimentPage] = ValueId("suggest-charts-mode")
SUGGEST_CONTENT_ID: DivId[ExperimentPage] = DivId("suggest-charts-content")
SUGGEST_SUGGESTIONS_STORE_ID: StoreId[ExperimentPage] = StoreId("suggest-charts-store")
SUGGEST_SCOPE_STORE_ID: StoreId[ExperimentPage] = StoreId("suggest-charts-scope")

# --- auto-generate modal, opened from an empty view (callbacks in `_chart_suggestions.py`) ---
AUTO_POPULATE_OPEN_ID: ButtonId[ExperimentPage] = ButtonId("auto-populate-open")
AUTO_POPULATE_MODAL_ID: ModalId[ExperimentPage] = ModalId("auto-populate-modal")
AUTO_POPULATE_DELIMITER_ID: ValueId[ExperimentPage] = ValueId("auto-populate-delimiter")
AUTO_POPULATE_MODE_ID: ValueId[ExperimentPage] = ValueId("auto-populate-mode")
AUTO_POPULATE_PREVIEW_ID: DivId[ExperimentPage] = DivId("auto-populate-preview")
AUTO_POPULATE_BUTTON_ID: ButtonId[ExperimentPage] = ButtonId("auto-populate-button")
AUTO_POPULATE_CANCEL_ID: ButtonId[ExperimentPage] = ButtonId("auto-populate-cancel")

_SPLIT_MODE_DATA = [{"label": "Prefix", "value": "prefix"}, {"label": "Suffix", "value": "suffix"}]
DEFAULT_DELIMITER: typing.Final = "/"

# --- rename-panel modal shell (callbacks in `_panel_controls.py`) ---
RENAME_PANEL_MODAL_ID: ModalId[ExperimentPage] = ModalId("rename-panel-modal")
RENAME_PANEL_TARGET_ID: StoreId[ExperimentPage] = StoreId("rename-panel-target")
RENAME_PANEL_NAME_INPUT_ID: ValueId[ExperimentPage] = ValueId("rename-panel-name-input")
RENAME_PANEL_ERROR_ID: DivId[ExperimentPage] = DivId("rename-panel-error")
RENAME_PANEL_SAVE_ID: ButtonId[ExperimentPage] = ButtonId("rename-panel-save")
RENAME_PANEL_CANCEL_ID: ButtonId[ExperimentPage] = ButtonId("rename-panel-cancel")


# --- new-tab modal shell (callbacks in `_panel_controls.py`) -- the *only* place a brand-new tab
# name can be typed; moving a panel to an already-existing tab is dragging it onto that tab in the
# tab bar instead (`.dl-tab-target`, see `_experiment_page_dragdrop.js` and `TAB_DROP_STORE_ID`). ---
NEW_TAB_BUTTON_ID: ButtonId[ExperimentPage] = ButtonId("new-tab-button")
NEW_TAB_MODAL_ID: ModalId[ExperimentPage] = ModalId("new-tab-modal")
NEW_TAB_NAME_INPUT_ID: ValueId[ExperimentPage] = ValueId("new-tab-name-input")
NEW_TAB_PANELS_SELECT_ID: ValueId[ExperimentPage] = ValueId("new-tab-panels-select")
NEW_TAB_ERROR_ID: DivId[ExperimentPage] = DivId("new-tab-error")
NEW_TAB_SAVE_ID: ButtonId[ExperimentPage] = ButtonId("new-tab-save")
NEW_TAB_CANCEL_ID: ButtonId[ExperimentPage] = ButtonId("new-tab-cancel")

# --- rename-tab modal shell (callbacks in `_panel_controls.py`) -- renames whichever tab is
# currently active (read off `PANEL_TABS_ID`'s own value), not a per-tab control. ---
RENAME_TAB_BUTTON_ID: ButtonId[ExperimentPage] = ButtonId("rename-tab-button")
RENAME_TAB_MODAL_ID: ModalId[ExperimentPage] = ModalId("rename-tab-modal")
RENAME_TAB_TARGET_ID: StoreId[ExperimentPage] = StoreId("rename-tab-target")
RENAME_TAB_NAME_INPUT_ID: ValueId[ExperimentPage] = ValueId("rename-tab-name-input")
RENAME_TAB_ERROR_ID: DivId[ExperimentPage] = DivId("rename-tab-error")
RENAME_TAB_SAVE_ID: ButtonId[ExperimentPage] = ButtonId("rename-tab-save")
RENAME_TAB_CANCEL_ID: ButtonId[ExperimentPage] = ButtonId("rename-tab-cancel")

# --- delete-panel / delete-chart confirm modal shells (callbacks in `_panel_controls.py`) ---
DELETE_PANEL_MODAL_ID: ModalId[ExperimentPage] = ModalId("delete-panel-modal")
DELETE_PANEL_TARGET_ID: StoreId[ExperimentPage] = StoreId("delete-panel-target")
DELETE_PANEL_CONFIRM_ID: ButtonId[ExperimentPage] = ButtonId("delete-panel-confirm")
DELETE_PANEL_CANCEL_ID: ButtonId[ExperimentPage] = ButtonId("delete-panel-cancel")

DELETE_CHART_MODAL_ID: ModalId[ExperimentPage] = ModalId("delete-chart-modal")
DELETE_CHART_TARGET_ID: StoreId[ExperimentPage] = StoreId("delete-chart-target")
DELETE_CHART_CONFIRM_ID: ButtonId[ExperimentPage] = ButtonId("delete-chart-confirm")
DELETE_CHART_CANCEL_ID: ButtonId[ExperimentPage] = ButtonId("delete-chart-cancel")


class ChartTargetData(TypedDict):
    panel: str
    index: int | None


class ChartID(ChartTargetData):
    type: str


class PanelReorderRequest(TypedDict):
    """A completed panel drag: move `panel` to just before/after `target`."""

    panel: str
    target: str
    after: bool


class TabDropRequest(TypedDict):
    """A panel dropped onto an existing tab: move `panel` to `tab` (`""` for the ungrouped tab)."""

    panel: str
    tab: str


class ChartTabDropRequest(TypedDict):
    """A chart dropped onto an existing tab: move the chart at `index` in `panel` to `tab`."""

    panel: str
    index: int
    tab: str


class ChartPanelMoveRequest(TypedDict):
    """
    A chart dropped into a *different* panel: move the chart at `index` in `panel` to `target_panel`.

    `target_index` is `None` when the drop landed on the target panel's body generally (an empty
    panel, or anywhere that isn't one of its existing charts) rather than on one of its charts --
    the chart is appended there instead of inserted at a particular position.
    """

    panel: str
    index: int
    target_panel: str
    target_index: int | None
    after: bool


class ChartReorderRequest(TypedDict):
    """A completed chart drag: move the chart at `index` in `panel` to just before/after `target_index`."""

    panel: str
    index: int
    target_index: int
    after: bool


def require_triggered_id() -> Any:  # noqa: ANN401
    """
    Return `ctx.triggered_id`, or raise `PreventUpdate` if nothing meaningfully triggered.

    Dash still fires pattern-matched callbacks when a listened component is created with its
    property at a falsy default (e.g. a freshly-rendered button's `n_clicks=0`); checking
    `ctx.triggered[0]["value"]` distinguishes that no-op firing from an actual click/change.
    Callers `cast(...)` the result to the triggered-id shape they expect.
    """
    if not ctx.triggered_id or not ctx.triggered[0]["value"]:  # pyright: ignore[reportUnknownMemberType]
        raise PreventUpdate
    return ctx.triggered_id  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def open_chart_button_id(panel_name: str) -> dict[str, str]:
    return {"type": "open-add-chart", "panel": panel_name}


def edit_chart_button_id(panel_name: str, index: int) -> ChartID:
    return {"type": "edit-chart", "panel": panel_name, "index": index}


def copy_chart_link_button_id(panel_name: str, index: int) -> ChartID:
    return {"type": "copy-chart-link", "panel": panel_name, "index": index}


def delete_chart_button_id(panel_name: str, index: int) -> ChartID:
    return {"type": "delete-chart", "panel": panel_name, "index": index}


def delete_panel_button_id(panel_name: str) -> dict[str, str]:
    return {"type": "delete-panel", "panel": panel_name}


def rename_panel_button_id(panel_name: str) -> dict[str, str]:
    return {"type": "rename-panel", "panel": panel_name}


PANEL_LAYOUT_TYPE: typing.Final = "panel-layout"
PANEL_GRID_COLUMNS_TYPE: typing.Final = "panel-grid-columns"
"""Pattern-matching id `type`s of a panel's Packed/Grid control and its grid column count (one callback serves both)."""


def panel_sync_switch_id(panel_name: str) -> dict[str, str]:
    return {"type": "panel-sync", "panel": panel_name}


def panel_layout_control_id(panel_name: str) -> dict[str, str]:
    return {"type": PANEL_LAYOUT_TYPE, "panel": panel_name}


def panel_grid_columns_id(panel_name: str) -> dict[str, str]:
    return {"type": PANEL_GRID_COLUMNS_TYPE, "panel": panel_name}


def panel_suggest_button_id(panel_name: str) -> dict[str, str]:
    return {"type": "panel-suggest-charts", "panel": panel_name}


def panel_drag_handle_id(panel_name: str) -> dict[str, str]:
    return {"type": "panel-drag-handle", "panel": panel_name}


def chart_drag_handle_id(panel_name: str, index: int) -> ChartID:
    return {"type": "chart-drag-handle", "panel": panel_name, "index": index}


def chart_controls_group_id(panel_name: str, index: int) -> ChartID:
    return {"type": "chart-controls", "panel": panel_name, "index": index}


def chart_content_id(panel_name: str, index: int) -> ChartID:
    return {"type": "chart-content", "panel": panel_name, "index": index}


def panel_content_id(panel_name: str) -> dict[str, str]:
    return {"type": "panel-content", "panel": panel_name}


def panel_accordion_id(tab: str) -> dict[str, str]:
    return {"type": "panel-accordion", "tab": tab}


def add_suggestion_button_id(kind: str, key: str) -> dict[str, str]:
    return {"type": "add-suggestion", "kind": kind, "key": key}


# ============================================================
# dataframe fetching
# ============================================================


def fetch_panel_dataframe(
    store: DataStore[...],
    experiment_id: int,
    panel: models.PanelInstance[Any, Any],
    page_settings: dict[str, Any],
) -> pd.DataFrame:
    """Fetch just the data one panel needs -- not the whole experiment."""
    metric_cols = panel.hint_required_columns()
    artifact_keys = panel.hint_required_artifact_keys()

    if (metric_cols is None or metric_cols) and artifact_keys:
        _log.warning(
            "Panel %r mixes metric and artifact charts, fetching both is "
            "less efficient than a panel of one kind; consider splitting it.",
            panel.name,
        )

    # Each hint is `None` for "everything" (e.g. a table chart with no `metrics` filter -- its normal
    # default, flagged to the user by `_unscoped_fetch_badge`) or a set of keys, where empty means
    # "none of this kind at all".
    hparam_keys = panel.hint_required_hparams()
    excluded = frozenset[int](page_settings.get(dfh.EXCLUDED_RUNS_KEY) or [])
    metrics_df = (
        store.fetch_metrics(
            experiment_id,
            keys=None if metric_cols is None else frozenset(metric_cols),
            exclude_run_ids=excluded,
        ).to_frame()
        if metric_cols is None or metric_cols
        else pd.DataFrame()
    )
    artifacts_df = (
        dfh.build_artifacts_dataframe(
            store.fetch_artifacts(
                experiment_id,
                keys=frozenset(k for k in artifact_keys if k is not None),
                exclude_run_ids=excluded,
            )
        )
        if artifact_keys
        else pd.DataFrame()
    )
    # fetch_hyperparams has no server-side key filter -- it's one small row per run either way.
    hparams_df = (
        dfh.build_hyperparams_dataframe(
            store.fetch_hyperparams(experiment_id, exclude_run_ids=excluded), keys=hparam_keys
        )
        if hparam_keys is None or hparam_keys
        else pd.DataFrame()
    )
    merged = dfh.merge_hyperparams(dfh.merge_metrics_and_artifacts(metrics_df, artifacts_df), hparams_df)
    if merged.empty:
        return merged
    # Carried in alongside the metric/artifact/hparam data (not fetched separately by each chart
    # that wants it) so `LineChart`/`ImageChart` can label a run by its name instead of its bare id
    # -- `table_chart.py`'s default "runs" mode explicitly excludes this column, since it isn't a
    # real metric/hparam. `store.get_runs`'s own default page size caps this at 1000 runs, same as
    # every other `list_*` read on this page; a larger experiment just leaves the extra runs'
    # `RUN_NAME_COLUMN` cell blank after the merge below.
    run_names = pd.DataFrame.from_records(
        [{"run_id": run.id, RUN_NAME_COLUMN: run_display_name(run)} for run in store.get_runs(experiment_id)]
    )
    return merged if run_names.empty else merged.merge(run_names, on="run_id", how="left")


def is_lightning_experiment(data_store: DataStore[...], experiment_id: int) -> bool:
    """
    Whether `experiment_id`'s metrics were logged through `DLTrackLogger`.

    Lightning-specific chart-autogen conventions (see `_chart_autogen.lightning_granularity`) only
    apply to those.
    """
    experiment = data_store.get_experiment(experiment_id)
    return experiment is not None and experiment.source == models.ExperimentSource.PYTORCH_LIGHTNING


# ============================================================
# panel/chart rendering
# ============================================================


def _render_chart_safely(
    chart: models.ChartInstance[Any, Any], dataframe: pd.DataFrame, panel_name: str
) -> Component:
    """Render one chart, isolating failures so a broken chart doesn't take the rest of the panel down."""
    try:
        return chart.render(dataframe)
    except Exception as exc:  # noqa: BLE001
        _log.exception("Failed to render %r chart in panel %r", chart.chart_type, panel_name)
        return dmc.Alert(
            f"{type(exc).__name__}: {exc}",
            title=f"Failed to render {chart.chart_type} chart",
            color="red",
            variant="light",
        )


def _apply_panel_sync(rendered: Component, *, panel_name: str, sync: bool) -> Component:
    """
    Scope a rendered line chart's crosshair sync to its own panel, or drop it if sync is off.

    A chart's own `syncId` is just its x-axis column name (e.g. "step"), shared by every line
    chart on the page with that x-axis -- not just the ones in the same panel. Two panels that
    both use "step" (e.g. train vs. val, whose step ranges don't correspond to each other) would
    otherwise wrongly sync their tooltips together.
    """
    if not isinstance(rendered, dmc.LineChart):
        return rendered
    untyped = cast("Any", rendered)
    props: dict[str, Any] = dict(untyped.lineChartProps or {})
    sync_id = props.pop("syncId", None)
    if sync and sync_id is not None:
        props["syncId"] = f"{panel_name}:{sync_id}"
    untyped.lineChartProps = props
    return rendered


def _unscoped_fetch_badge(chart: models.ChartInstance[Any, Any]) -> Component | None:
    """
    A small, always-visible warning badge for a chart whose data fetch can't be scoped.

    `chart.hint_required_columns() is None` means this chart's settings don't name specific
    metrics (e.g. a table chart with no `metrics` filter, left at its "show every metric" default)
    -- rendering it means fetching every metric logged for the whole experiment, every time its
    panel updates. That's a real, sometimes-significant cost the user has no other way to notice
    (it only ever shows up as a backend debug log), so it gets a badge instead of hover-only
    controls: this should be visible without having to go looking for it.
    """
    if chart.hint_required_columns() is not None:
        return None
    return dmc.Tooltip(
        dmc.Badge("fetches all metrics", color="yellow", variant="light", size="xs", radius="sm"),
        label="No metrics filter is set, so this chart fetches every metric in the experiment on "
        'every update -- can be slow on a large experiment. Set specific metrics in "Edit chart" '
        "to scope it down.",
        position="top",
        withArrow=True,
        multiline=True,
        w=260,
    )


def live_status_badge(*, ok: bool) -> Component:
    """
    Small colored chip reporting whether the live-update poll (`poll_for_updates`) is healthy.

    Stateless by design: a single failed poll is enough to flip it red, and it self-clears green on
    the very next successful one -- no failure-count/session state to track or leak, matching this
    feature's "don't trust clients, don't hold server-side connection state" approach.
    """
    return (
        dmc.Badge("Live", color="green", variant="dot", size="sm", tt="none")
        if ok
        else dmc.Badge("Reconnecting", color="red", variant="dot", size="sm", tt="none")
    )


def render_chart_item(
    panel: models.PanelInstance[Any, Any], index: int, dataframe: pd.DataFrame
) -> Component:
    """
    Render one chart in a panel (edit/delete controls above it, revealed on hover) as its own node.

    Individually addressable via `chart_content_id(panel.name, index)` -- a live-update poll
    (`poll_for_updates` in `__init__.py`) patches exactly this node when this specific chart's own
    data changes, instead of rebuilding `render_panel_charts`' whole shared-dataframe blob for the
    panel: two charts sharing a panel (a common "Auto-generate charts" grouping) commonly have very
    different update cadences -- an epoch-level validation chart next to a per-step training chart
    -- and rebuilding the *panel* whenever *either* changes would pop the untouched one too.
    """
    chart = panel.charts[index]
    rendered = _apply_panel_sync(
        _render_chart_safely(chart, dataframe, panel.name), panel_name=panel.name, sync=panel.sync
    )
    hover_controls = dmc.Group(
        [
            _drag_handle(
                class_name="dl-chart-drag-handle",
                component_id=chart_drag_handle_id(panel.name, index),  # pyright: ignore[reportArgumentType]
                data_attrs={"panel-name": panel.name, "chart-index": str(index)},
            ),
            tooltipped_action_icon(
                Icon.EDIT,
                component_id=edit_chart_button_id(panel.name, index),  # pyright: ignore[reportArgumentType]
                label="Edit chart",
                size="xs",
            ),
            # Copied client-side by `chart_deep_link.js`, from the page's own URL -- no round trip.
            tooltipped_action_icon(
                Icon.LINK,
                component_id=copy_chart_link_button_id(panel.name, index),  # pyright: ignore[reportArgumentType]
                label="Copy a link to this chart",
                size="xs",
                **cast("dict[str, Any]", {"data-dl-copy-chart-link": chart.id}),
            ),
            tooltipped_action_icon(
                Icon.DELETE,
                component_id=delete_chart_button_id(panel.name, index),  # pyright: ignore[reportArgumentType]
                label="Delete chart",
                color="red",
                size="xs",
            ),
        ],
        id=chart_controls_group_id(panel.name, index),  # pyright: ignore[reportArgumentType]
        justify="flex-end",
        gap="xs",
        className="dl-chart-controls",
    )
    # The badge (when present) must be a *sibling* of `hover_controls`, not nested inside it --
    # `dl-chart-controls`'s opacity:0-until-hover applies to its whole subtree, and this badge
    # is the one thing here that's supposed to stay visible without hovering.
    badge = _unscoped_fetch_badge(chart)
    header_row = dmc.Group(
        [*([badge] if badge else []), hover_controls],
        justify="space-between" if badge else "flex-end",
        wrap="nowrap",
    )
    return dmc.Stack(
        [header_row, rendered],
        id=chart_content_id(panel.name, index),  # pyright: ignore[reportArgumentType]
        gap="xs",
        w="100%" if panel.layout == "grid" else chart.natural_width(),
        maw="100%",
        p="xs",
        className="dl-chart-item",
        **cast("dict[str, Any]", {"data-chart-id": chart.id}),
    )


def render_panel_charts(panel: models.PanelInstance[Any, Any], dataframe: pd.DataFrame) -> list[Component]:
    """Render every chart in a panel -- see `render_chart_item`."""
    return [render_chart_item(panel, idx, dataframe) for idx in range(len(panel.charts))]


def render_panel_content_from_df(panel: models.PanelInstance[Any, Any], df: pd.DataFrame) -> Component:
    """Render a panel's charts from an already-fetched dataframe -- see `render_panel_content`."""
    items = render_panel_charts(panel, df)
    return (
        dmc.SimpleGrid(items, cols=panel.grid_columns, spacing="lg")
        if panel.layout == "grid"
        else dmc.Flex(items, justify="flex-start", gap="lg", wrap="wrap")
    )


def render_panel_content(
    store: DataStore[...],
    experiment_id: int,
    panel: models.PanelInstance[Any, Any],
    page_settings: dict[str, Any],
) -> Component:
    """Fetch + render one panel's charts. Only called for panels that are actually open."""
    df = fetch_panel_dataframe(store, experiment_id, panel, page_settings)
    return render_panel_content_from_df(panel, df)


def chart_data_fingerprint(chart: models.ChartInstance[Any, Any], df: pd.DataFrame) -> str:
    """
    A cheap content hash of the slice of a panel's shared dataframe one chart actually renders from.

    Lets a live-update poll tell "this chart's underlying data hasn't actually changed" apart from
    "the experiment changed *something*, somewhere" -- `Experiment.revision` is bumped by any write
    anywhere in the experiment (a different run, a different metric, an epoch-level chart's data
    between epochs), so without this every open chart would re-render on every poll tick regardless
    of whether anything it actually shows moved. Scoped per *chart*, not per panel: a panel commonly
    holds charts with very different update cadences -- an epoch-level chart next to a per-step one,
    grouped there by "Auto-generate charts" -- and a panel-wide fingerprint would pop every chart in
    it whenever *any one* of them changed.

    Falls back to hashing the whole (already panel-scoped) dataframe when `hint_required_columns()`
    is `None` -- a chart whose own columns aren't knowable upfront (e.g. a table chart with no
    metrics filter, documented to mean "show every metric") can't be scoped any tighter than the
    panel fetch already was.

    Hashes the `to_json(orient="split", ...)` serialization the rest of this page relies on,
    not `pandas.util.hash_pandas_object` -- that hashes cell values
    directly and raises `TypeError: unhashable type` the moment any column holds something like a
    list (nothing in this dataframe's own columns does today, but a hash used purely to detect "did
    this change" has no business being pickier about content than the JSON serialization the rest of
    this page already ships to the browser).
    """
    cols = chart.hint_required_columns()
    scoped = df if cols is None else df[[c for c in cols if c in df.columns]]
    return hashlib.sha1(scoped.to_json(orient="split", date_format="iso").encode()).hexdigest()


def _panel_placeholder() -> dmc.Skeleton:
    return dmc.Skeleton(height=60, radius="sm")


def _drag_handle(*, class_name: str, component_id: dict[str, str], data_attrs: dict[str, str]) -> Component:
    """A tooltipped grip glyph, draggable via `_experiment_page_dragdrop.js`."""
    # `data-*` attrs are only known dynamically (dict keys, not literal kwargs), so pyright can't
    # match them against `html.Div`'s typed signature -- `cast` to `Any` rather than fight that.
    div = cast("Any", html.Div)
    return dmc.Tooltip(
        div(
            icon(Icon.DRAG),
            id=component_id,
            draggable="true",
            className=class_name,
            **{f"data-{key}": value for key, value in data_attrs.items()},
        ),
        label="Drag to reorder",
        position="top",
        withArrow=True,
    )


def panel_header_controls(panel: models.PanelInstance[Any, Any]) -> Component:
    """
    Sync/layout/rename/drag/suggest/delete for one panel, hover-revealed on the panel's own header.

    Moving a panel to a tab is drag-and-drop (drag the same handle used to reorder panels onto a
    tab in the tab bar, see `_experiment_page_dragdrop.js`), not a control here.
    """
    panel_name = panel.name
    return dmc.Group(
        [
            _drag_handle(
                class_name="dl-panel-drag-handle",
                component_id=panel_drag_handle_id(panel_name),
                data_attrs={"panel-name": panel_name},
            ),
            tooltipped_action_icon(
                Icon.ADD, component_id=open_chart_button_id(panel_name), label="Add chart to this panel"
            ),
            tooltipped_action_icon(
                Icon.SUGGEST,
                component_id=panel_suggest_button_id(panel_name),
                label="Suggest charts for this panel",
            ),
            dmc.Divider(orientation="vertical"),
            dmc.Tooltip(
                dmc.Switch(
                    id=panel_sync_switch_id(panel_name),
                    label="Sync",
                    checked=panel.sync,
                    size="xs",
                ),
                label="Sync the crosshair/tooltip across this panel's charts that share an x-axis",
                position="top",
                withArrow=True,
            ),
            dmc.Tooltip(
                dmc.SegmentedControl(
                    id=panel_layout_control_id(panel_name),
                    data=[
                        {"value": "packed", "label": "Packed"},
                        {"value": "grid", "label": "Grid"},
                    ],
                    value=panel.layout,
                    size="xs",
                ),
                label="Packed: charts sized to their own natural width. Grid: charts stretch to fill equal-width columns",
                position="top",
                withArrow=True,
            ),
            *(
                [
                    dmc.Tooltip(
                        dmc.NumberInput(
                            id=panel_grid_columns_id(panel_name),
                            value=panel.grid_columns,
                            min=models.MIN_GRID_COLUMNS,
                            max=models.MAX_GRID_COLUMNS,
                            allowDecimal=False,
                            allowNegative=False,
                            clampBehavior="strict",
                            size="xs",
                            w=56,
                            **cast("dict[str, Any]", {"aria-label": "Grid columns"}),
                        ),
                        label="How many columns the grid has",
                        position="top",
                        withArrow=True,
                    )
                ]
                if panel.layout == "grid"
                else []
            ),
            tooltipped_action_icon(
                Icon.EDIT, component_id=rename_panel_button_id(panel_name), label="Rename panel"
            ),
            tooltipped_action_icon(
                Icon.DELETE,
                component_id=delete_panel_button_id(panel_name),
                label="Delete panel",
                color="red",
            ),
        ],
        className="dl-panel-controls",
        gap="xs",
        wrap="nowrap",
    )


def panel_header(panel: models.PanelInstance[Any, Any]) -> Component:
    """
    A panel's accordion header.

    `AccordionControl` plus hover-revealed controls as true DOM siblings (both direct children of
    this wrapping `Group`) -- not nested inside the control's own `<button>`, which native HTML
    doesn't allow another interactive element inside anyway.
    """
    return dmc.Group(
        [
            dmc.AccordionControl(
                [dmc.Text(panel.name, size="xs", fw=600)],
                py="xs",
                px="xs",
                style={"flex": 1},
            ),
            panel_header_controls(panel),
        ],
        className="dl-panel-item-header",
        gap=0,
        wrap="nowrap",
    )


# ============================================================
# page model
# ============================================================


def _group_panels_by_tab(
    panels: list[models.PanelInstance[Any, Any]],
) -> dict[str, list[models.PanelInstance[Any, Any]]]:
    """Group panels by `tab`, preserving first-seen tab order (the ungrouped tab always sorts first)."""
    groups: dict[str, list[models.PanelInstance[Any, Any]]] = {_UNGROUPED_TAB: []}
    for p in panels:
        groups.setdefault(p.tab, []).append(p)
    return groups


class BasicExperimentPage(models.Page[pd.DataFrame, Component, html.Div], frozen=True, extra="forbid"):
    """Basic experiment page: per-panel charts rendered in an accordion, grouped into tabs."""

    @typing.override
    def render(self, data_store: DataStore[...], experiment_id: int) -> Component:
        """
        Render the tab-grouped accordions. Only open panels get real content; closed ones get a placeholder.

        Panels grouped under the same `tab` share one `Accordion`. If every panel shares the
        (default) ungrouped tab -- the common case today -- no `Tabs` chrome is shown at all, so
        this looks exactly like a plain accordion until a panel actually gets tabbed. Above the
        panels sits one toolbar row: tabs on the left, followed by the labeled "New tab" button,
        the *only* place a brand-new tab gets created (see `NEW_TAB_BUTTON_ID`) -- moving a panel to
        one that already exists is dragging its drag handle onto that tab (`.dl-tab-target`, see
        `_experiment_page_dragdrop.js`), the same handle used to reorder panels. Panel actions sit on
        the right. A view with no panels at all shows an empty state offering to auto-generate them.
        """
        new_tab_button = dmc.Button(
            "New tab",
            id=NEW_TAB_BUTTON_ID,
            leftSection=icon(Icon.ADD),
            variant="subtle",
            color="gray",
            size="xs",
        )
        if not self.panels:
            return html.Div([_toolbar([new_tab_button], [_new_panel_popover()]), _empty_view()])

        open_value = self.page_settings.get(OPEN_PANEL_KEY, [self.panels[0].name])
        if isinstance(open_value, str):
            open_value = [open_value]
        open_set: set[str] = set(cast("list[str]", open_value))

        groups = _group_panels_by_tab(self.panels)
        accordions = {
            tab: _panel_accordion(data_store, experiment_id, tab, group, open_set, self.page_settings)
            for tab, group in groups.items()
        }
        panel_actions: list[Component] = [_new_panel_popover(), _suggest_charts_button()]
        if len(accordions) <= 1:
            return html.Div([_toolbar([new_tab_button], panel_actions), next(iter(accordions.values()))])

        active_tab = self.page_settings.get(ACTIVE_TAB_KEY) or next(iter(accordions))
        if active_tab not in accordions:
            active_tab = next(iter(accordions))
        rename_tab_button = tooltipped_action_icon(
            Icon.EDIT,
            component_id=RENAME_TAB_BUTTON_ID,
            label="Rename the active tab",
            size="xs",
            disabled=active_tab == _UNGROUPED_TAB,
        )
        return dmc.Tabs(
            id=PANEL_TABS_ID,
            value=tab_component_value(active_tab),
            children=[
                _toolbar(
                    [dmc.TabsList([_tab_tab(tab) for tab in accordions]), rename_tab_button, new_tab_button],
                    panel_actions,
                ),
                *(
                    dmc.TabsPanel(accordion, value=tab_component_value(tab))
                    for tab, accordion in accordions.items()
                ),
            ],
        )


def _toolbar(tab_controls: list[Component], panel_actions: list[Component]) -> dmc.Group:
    """
    The row above the panels: tab controls on the left, panel actions on the right.

    Part of the same full-width tree as the panels rather than a container off to the side, which
    squeezed the panels into sharing a row with it.
    """
    return dmc.Group(
        [dmc.Group(tab_controls, gap="xs", wrap="nowrap"), dmc.Group(panel_actions, gap="xs", wrap="nowrap")],
        justify="space-between",
        wrap="nowrap",
        gap="xs",
        mb="sm",
    )


def _tab_tab(tab: str) -> Component:
    """One `TabsTab`, also a drag-and-drop target for moving a panel to this (existing) tab."""
    # `data-*` attrs aren't in `TabsTab`'s typed signature -- `cast` to `Any` rather than fight that.
    tabs_tab = cast("Any", dmc.TabsTab)
    return tabs_tab(
        tab or "General", value=tab_component_value(tab), className="dl-tab-target", **{"data-tab": tab}
    )


def _panel_accordion(  # noqa: PLR0913
    data_store: DataStore[...],
    experiment_id: int,
    tab: str,
    panels: list[models.PanelInstance[Any, Any]],
    open_set: set[str],
    page_settings: dict[str, Any],
) -> dmc.Accordion:
    return dmc.Accordion(
        id=panel_accordion_id(tab),
        multiple=True,
        value=[p.name for p in panels if p.name in open_set],
        variant="separated",
        className="dl-panels",
        chevronPosition="left",
        children=[
            dmc.AccordionItem(
                [
                    panel_header(p),
                    dmc.AccordionPanel(
                        # `data-panel-name` isn't in `html.Div`'s typed signature -- `cast` to `Any`
                        # rather than fight that (same as `_drag_handle`'s data attrs). Read by
                        # `_experiment_page_dragdrop.js` to let a dragged chart be dropped anywhere
                        # in a *different* panel's body, not only onto one of its existing charts.
                        #
                        # This div's own children must stay exactly `render_panel_content(...)`'s
                        # output (or the placeholder) -- `poll_for_updates` (`_experiment/__init__.py`)
                        # patches live-update data straight into each individual chart's own
                        # `chart_content_id(...)` node inside here, bypassing this whole
                        # `_panel_accordion` tree. Any chrome added here later (e.g. a per-panel
                        # "last updated" badge) that isn't also part of `render_panel_content`'s
                        # output would vanish the next time a live-update poll patches an open panel.
                        cast("Any", html.Div)(
                            id=panel_content_id(p.name),
                            className="dl-panel-body",
                            **{"data-panel-name": p.name},
                            children=(
                                render_panel_content(data_store, experiment_id, p, page_settings)
                                if p.name in open_set
                                else _panel_placeholder()
                            ),
                        ),
                        px="xs",
                        py="xs",
                    ),
                ],
                p.name,
            )
            for p in panels
        ],
    )


# ============================================================
# static shells: add/edit-chart modal, suggest-charts drawer, rename/delete confirm modals
#
# These are always present in `accordion_view`'s output (opened=False until a controller callback
# opens them), so they have to live where `accordion_view` can reach them without a cross-import
# back into the controller modules that own their *behavior*.
# ============================================================


def split_mode_control(control_id: ValueId[ExperimentPage]) -> dmc.SegmentedControl:
    return dmc.SegmentedControl(id=control_id, data=_SPLIT_MODE_DATA, value="prefix", size="sm")


def _new_panel_popover() -> dmc.Popover:
    """
    The toolbar's "New panel" button, and the name form it opens right beneath it.

    Creating the panel re-renders the whole panel area, this popover included, which is what
    closes it -- no open/close state to keep. A bad name leaves it open with the error inline.
    """
    return dmc.Popover(
        [
            dmc.PopoverTarget(
                dmc.Button(
                    "New panel", id=NEW_PANEL_OPEN_ID, leftSection=icon(Icon.ADD), variant="light", size="xs"
                )
            ),
            dmc.PopoverDropdown(
                dmc.Stack(
                    [
                        dmc.TextInput(
                            id=NEW_PANEL_NAME_ID,
                            label="Panel name",
                            placeholder="e.g. Losses",
                            size="xs",
                            **cast("dict[str, Any]", {"data-autofocus": True}),
                        ),
                        dmc.Group(
                            dmc.Button("Create panel", id=NEW_PANEL_ID, n_clicks=0, size="xs"),
                            justify="flex-end",
                        ),
                    ],
                    gap="xs",
                )
            ),
        ],
        position="bottom-end",
        width=260,
        withArrow=True,
        shadow="md",
        trapFocus=True,
    )


def _suggest_charts_button() -> dmc.Button:
    return dmc.Button(
        "Suggest charts",
        id=SUGGEST_CHARTS_BUTTON_ID,
        n_clicks=0,
        leftSection=icon(Icon.SUGGEST),
        variant="subtle",
        size="xs",
    )


def _empty_view() -> Component:
    """A view with no panels: offer to auto-generate them, behind a modal that previews the result first."""
    return html.Div(
        [
            dmc.Paper(
                dmc.Stack(
                    [
                        dmc.ThemeIcon(icon(Icon.SUGGEST), size="xl", radius="xl", variant="light"),
                        dmc.Text("No charts in this view yet", fw=600, size="lg"),
                        dmc.Text(
                            "Auto-generate a panel for each group of logged metrics and artifacts, "
                            "or start from an empty panel with New panel.",
                            c="dimmed",
                            size="sm",
                            ta="center",
                            maw=440,
                        ),
                        dmc.Button(
                            "Auto-generate charts",
                            id=AUTO_POPULATE_OPEN_ID,
                            n_clicks=0,
                            leftSection=icon(Icon.SUGGEST),
                            variant="gradient",
                            mt="xs",
                        ),
                    ],
                    align="center",
                    gap="xs",
                ),
                withBorder=True,
                radius="md",
                p="xl",
                style={"borderStyle": "dashed"},
            ),
            dmc.Modal(
                id=AUTO_POPULATE_MODAL_ID,
                title="Auto-generate charts",
                size="lg",
                opened=False,
                children=dmc.Stack(
                    [
                        dmc.Text(
                            "Each metric and artifact name is split on the delimiter, and grouped into a "
                            "panel by its first part (prefix) or last part (suffix).",
                            size="sm",
                            c="dimmed",
                        ),
                        dmc.Group(
                            [
                                dmc.TextInput(
                                    id=AUTO_POPULATE_DELIMITER_ID,
                                    label="Delimiter",
                                    value=DEFAULT_DELIMITER,
                                    w=90,
                                    size="sm",
                                ),
                                dmc.Stack(
                                    [
                                        dmc.Text("Group by", size="sm", fw=500),
                                        split_mode_control(AUTO_POPULATE_MODE_ID),
                                    ],
                                    gap=4,
                                ),
                            ],
                            align="flex-end",
                        ),
                        html.Div(id=AUTO_POPULATE_PREVIEW_ID),
                        dmc.Group(
                            [
                                dmc.Button("Cancel", id=AUTO_POPULATE_CANCEL_ID, variant="default"),
                                dmc.Button("Create charts", id=AUTO_POPULATE_BUTTON_ID, n_clicks=0),
                            ],
                            justify="flex-end",
                        ),
                    ]
                ),
            ),
        ]
    )


def _suggest_charts_drawer() -> dmc.Drawer:
    return dmc.Drawer(
        id=SUGGEST_DRAWER_ID,
        title="Suggested charts",
        position="right",
        size="md",
        opened=False,
        children=[
            Store(id=SUGGEST_SUGGESTIONS_STORE_ID, data=[]),
            Store(id=SUGGEST_SCOPE_STORE_ID, data=None),
            dmc.Group(
                [
                    dmc.TextInput(
                        id=SUGGEST_DELIMITER_ID, label="Delimiter", value=DEFAULT_DELIMITER, w=90, size="sm"
                    ),
                    split_mode_control(SUGGEST_MODE_ID),
                ],
                gap="sm",
                mb="sm",
            ),
            html.Div(id=SUGGEST_CONTENT_ID),
        ],
    )


def _add_chart_modal() -> dmc.Modal:
    chart_types = sorted(models.ChartTypeRegistry.get_registered_chart_types().keys())
    return dmc.Modal(
        id=ADD_CHART_MODAL_ID,
        title="Add chart",
        opened=False,
        size="lg",
        children=[
            Store(id=ADD_CHART_TARGET_ID),
            Store(id=ADD_CHART_INITIAL_PARAMS_ID),
            dmc.Stack(
                [
                    dmc.Select(id=ADD_CHART_TYPE_SELECT_ID, label="Chart type", data=chart_types, value=None),
                    html.Div(id=ADD_CHART_PARAMS_ID),
                    dmc.Text("Preview", fw=600, mt="sm"),
                    html.Div(id=ADD_CHART_PREVIEW_ID),
                    dmc.Text(id=ADD_CHART_ERROR_ID, c="red", size="sm"),
                    dmc.Group(
                        [
                            dmc.Button("Cancel", id=ADD_CHART_CANCEL_ID, variant="default"),
                            dmc.Button("Add chart", id=ADD_CHART_SUBMIT_ID),
                        ],
                        justify="flex-end",
                    ),
                ],
            ),
        ],
    )


def _rename_panel_modal() -> dmc.Modal:
    return dmc.Modal(
        id=RENAME_PANEL_MODAL_ID,
        title="Rename panel",
        opened=False,
        children=[
            Store(id=RENAME_PANEL_TARGET_ID),
            dmc.Stack(
                [
                    dmc.TextInput(id=RENAME_PANEL_NAME_INPUT_ID, label="Panel name"),
                    dmc.Text(id=RENAME_PANEL_ERROR_ID, c="red", size="sm"),
                    dmc.Group(
                        [
                            dmc.Button("Cancel", id=RENAME_PANEL_CANCEL_ID, variant="default"),
                            dmc.Button("Save", id=RENAME_PANEL_SAVE_ID),
                        ],
                        justify="flex-end",
                    ),
                ],
            ),
        ],
    )


def _new_tab_modal() -> dmc.Modal:
    """
    The *only* place a brand-new tab gets created (see `NEW_TAB_BUTTON_ID`).

    Name it and, optionally, pick existing panels to move into it -- leave the picker empty and the
    tab starts with one fresh, empty panel instead. Moving one more panel in later, or moving a
    single chart into an existing tab, is drag-and-drop (`.dl-tab-target`), not this modal, which
    only ever creates a brand-new tab.
    """
    return dmc.Modal(
        id=NEW_TAB_MODAL_ID,
        title="New tab",
        opened=False,
        children=[
            dmc.Stack(
                [
                    dmc.TextInput(id=NEW_TAB_NAME_INPUT_ID, label="Tab name"),
                    dmc.MultiSelect(
                        id=NEW_TAB_PANELS_SELECT_ID,
                        label="Panels",
                        description="Which panels should move into this tab (optional -- leave "
                        "empty to start with a new, empty panel)",
                        data=[],
                        searchable=True,
                    ),
                    dmc.Text(id=NEW_TAB_ERROR_ID, c="red", size="sm"),
                    dmc.Group(
                        [
                            dmc.Button("Cancel", id=NEW_TAB_CANCEL_ID, variant="default"),
                            dmc.Button("Create", id=NEW_TAB_SAVE_ID),
                        ],
                        justify="flex-end",
                    ),
                ],
            ),
        ],
    )


def _rename_tab_modal() -> dmc.Modal:
    """Renames whichever tab is active when `RENAME_TAB_BUTTON_ID` is clicked -- disabled on "General"."""
    return dmc.Modal(
        id=RENAME_TAB_MODAL_ID,
        title="Rename tab",
        opened=False,
        children=[
            Store(id=RENAME_TAB_TARGET_ID),
            dmc.Stack(
                [
                    dmc.TextInput(id=RENAME_TAB_NAME_INPUT_ID, label="Tab name"),
                    dmc.Text(id=RENAME_TAB_ERROR_ID, c="red", size="sm"),
                    dmc.Group(
                        [
                            dmc.Button("Cancel", id=RENAME_TAB_CANCEL_ID, variant="default"),
                            dmc.Button("Save", id=RENAME_TAB_SAVE_ID),
                        ],
                        justify="flex-end",
                    ),
                ],
            ),
        ],
    )


def _delete_panel_confirm_modal() -> dmc.Modal:
    return dmc.Modal(
        id=DELETE_PANEL_MODAL_ID,
        title="Delete this panel?",
        opened=False,
        children=[
            Store(id=DELETE_PANEL_TARGET_ID),
            dmc.Text("This deletes the panel and every chart in it. This can't be undone."),
            dmc.Group(
                [
                    dmc.Button("Cancel", id=DELETE_PANEL_CANCEL_ID, variant="default"),
                    dmc.Button("Delete", id=DELETE_PANEL_CONFIRM_ID, color="red"),
                ],
                justify="flex-end",
                mt="sm",
            ),
        ],
    )


def _delete_chart_confirm_modal() -> dmc.Modal:
    return dmc.Modal(
        id=DELETE_CHART_MODAL_ID,
        title="Delete this chart?",
        opened=False,
        children=[
            Store(id=DELETE_CHART_TARGET_ID),
            dmc.Text("This can't be undone."),
            dmc.Group(
                [
                    dmc.Button("Cancel", id=DELETE_CHART_CANCEL_ID, variant="default"),
                    dmc.Button("Delete", id=DELETE_CHART_CONFIRM_ID, color="red"),
                ],
                justify="flex-end",
                mt="sm",
            ),
        ],
    )


def _focus_on_chart(page: BasicExperimentPage, chart_id: str) -> BasicExperimentPage:
    """
    `page` as rendered for a `?chart=` deep link: that chart's panel open, and its tab active.

    Only for this one render, never persisted -- following a link someone shared mustn't change the
    experiment's layout for everyone else. An id that matches no chart leaves `page` as it is.
    """
    panel = next((p for p in page.panels if any(c.id == chart_id for c in p.charts)), None)
    if panel is None:
        return page
    open_value = page.page_settings.get(OPEN_PANEL_KEY, [page.panels[0].name])
    open_panels = [open_value] if isinstance(open_value, str) else cast("list[str]", open_value or [])
    return page.model_copy(
        update={
            "page_settings": {
                **page.page_settings,
                OPEN_PANEL_KEY: [*open_panels, panel.name],
                ACTIVE_TAB_KEY: panel.tab,
            }
        }
    )


class PageRef(typing.NamedTuple):
    """
    Which page an experiment-page callback reads and writes: the shared one, or a named view of it.

    Every callback that loads the page by experiment builds one of these from `STATE_EXPERIMENT_ID`
    and `STATE_VIEW_ID`, so an edit made while looking at a view lands in that view, never the
    shared page. (Callbacks that already hold the page itself -- `STATE_PAGE_STORAGE` -- write it back
    by its own id, which is right either way.)
    """

    experiment_id: int
    view_id: int | None


def load_page(store: DataStore[...], ref: PageRef) -> BasicExperimentPage:
    """The page `ref` points at; the shared page if its view no longer exists or isn't this experiment's."""
    if ref.view_id is not None:
        view = store.get_view(BasicExperimentPage, ref.view_id)
        if view is not None and view.experiment_id == ref.experiment_id:
            return cast("BasicExperimentPage", view)
    return cast(
        "BasicExperimentPage", store.get_or_create_page(BasicExperimentPage, experiment_id=ref.experiment_id)
    )


def ref_of(experiment_id: int, page: BasicExperimentPage) -> PageRef:
    """
    The `PageRef` that an already-loaded `page` itself represents.

    The shared page if it's nobody's, otherwise its own view id. A callback that reacts to
    `STATE_PAGE_STORAGE` (rather than to some other trigger that only
    incidentally carries `page`) should build its `PageRef` this way instead of from `STATE_VIEW_ID`:
    both changes land in the *same* server response as a structural edit that just branched into a
    new view, but `STATE_PAGE_STORAGE` is one of that response's own outputs, while `STATE_VIEW_ID`
    is only set by a *separate*, slightly later round trip (`_views.py`'s `sync_view_after_edit`) --
    reading it back too early would still see the view you were on before the edit branched.
    """
    return PageRef(experiment_id, page.id if page.owner_id is not None else None)


def accordion_view(
    store: DataStore[...], page: BasicExperimentPage, *, focus_chart: str | None = None
) -> html.Div:
    """
    The full accordion/tabs view of `page`, plus every modal/drawer shell it can open.

    Takes the page itself rather than re-reading it: every caller already has the version it just
    loaded or saved, which is also the only way to be sure a view is rendered, not the shared page.

    `focus_chart` (a `ChartInstance.id`, from a `?chart=` deep link) opens that chart's panel and
    tab for this render -- see `_focus_on_chart`.
    """
    experiment_id = typing.cast("int", page.experiment_id)
    _log.debug("rendering chart for experiment %s", experiment_id)
    if focus_chart:
        page = _focus_on_chart(page, focus_chart)
    open_value = page.page_settings.get(OPEN_PANEL_KEY, [page.panels[0].name] if page.panels else [])
    if isinstance(open_value, str):
        open_value = [open_value]

    return html.Div(
        [
            page.render(store, experiment_id),
            Store(id=LOADED_PANELS_STORE_ID, data=list(open_value)),  # pyright: ignore[reportArgumentType]
            _add_chart_modal(),
            _suggest_charts_drawer(),
            _rename_panel_modal(),
            _new_tab_modal(),
            _rename_tab_modal(),
            _delete_panel_confirm_modal(),
            _delete_chart_confirm_modal(),
        ],
    )


# ============================================================
# page-mutation primitives (shared by every controller module)
# ============================================================


_FORK_NAME: typing.Final = "My view"
"""
What a view an edit branched into is called (numbered if you already have one). Named for whose it
is, not "Copy of ...": those stacked up as "Copy of Copy of Shared view" and said nothing.
"""


def unique_view_name(store: DataStore[...], experiment_id: int, owner_id: int) -> str:
    """The first of "My view", "My view 2", ... that `owner_id` hasn't used for a view of this experiment yet."""
    taken = {view.name for view in store.list_views(experiment_id, owner_id)}
    candidates = (_FORK_NAME if n == 1 else f"{_FORK_NAME} {n}" for n in itertools.count(1))
    return next(name for name in candidates if name not in taken)


def save_page(store: DataStore[...], mutated: BasicExperimentPage) -> BasicExperimentPage:
    """
    Persist `mutated`, branching into a new view of your own first if you don't already own it.

    Written in place if you already own the page it came from, otherwise created as a brand-new
    view of your own, seeded with `mutated`'s own (already-edited) content. "Owning" it means its
    `owner_id` is your own user id; the shared page (`owner_id` is `None`) and a view someone else
    owns both fail that check. This is what lets an edit made while looking at the shared page, or a
    view that isn't yours, branch into a view of your own the moment you make it -- no separate
    "save as a view" step first, and nobody else's page is ever changed by an edit that isn't theirs.
    """
    user_id = get_current_user().id
    if mutated.owner_id == user_id:
        return cast("BasicExperimentPage", store.update_page(mutated))
    experiment_id = cast("int", mutated.experiment_id)
    return cast(
        "BasicExperimentPage",
        store.create_view(
            BasicExperimentPage,
            models.NewPage[Any, Any](
                experiment_id=experiment_id,
                owner_id=user_id,
                name=unique_view_name(store, experiment_id, user_id),
                panels=mutated.panels,
                page_settings=mutated.page_settings,
            ),
        ),
    )


def persist_settings(
    store: DataStore[...], ref: PageRef, updates: dict[str, Any], *, branch_on_edit: bool = True
) -> BasicExperimentPage:
    """
    Merge `updates` into page_settings (server-authoritative) and persist -- no accordion rebuild.

    `branch_on_edit` (on by default) branches into a view of your own first if `ref` doesn't already
    point at one you own -- see `save_page`. The two call sites that pass `False` (which panel/tab
    is open) are purely per-viewer browsing state: opening a panel isn't "editing the view" the way
    deleting a chart or excluding a run is, so those two keep writing straight to whatever page
    `ref` names, exactly as before this existed.
    """
    page = load_page(store, ref)
    new_settings = {**page.page_settings, **updates}
    mutated = page.model_copy(update={"page_settings": new_settings})
    if not branch_on_edit:
        return cast("BasicExperimentPage", store.update_page(mutated))
    return save_page(store, mutated)


def persist_settings_and_rerender(
    store: DataStore[...],
    ref: PageRef,
    updates: dict[str, Any],
) -> tuple[BasicExperimentPage, html.Div]:
    """Merge `updates` into page_settings (server-authoritative), persist, and re-render the accordion."""
    page = persist_settings(store, ref, updates)
    return page, accordion_view(store, page)


def mutate_panels_and_rerender(
    page_json: str,
    mutate: Callable[[list[models.PanelInstance[Any, Any]]], list[models.PanelInstance[Any, Any]]],
    *,
    extra_settings: dict[str, Any] | None = None,
) -> tuple[BasicExperimentPage, html.Div]:
    """
    Load page from client-cached state, apply `mutate` to its panels, persist, and re-render.

    `extra_settings` merges into `page_settings` alongside the panel mutation -- e.g. moving a chart
    or panel to a different tab also switches `ACTIVE_TAB_KEY` to it, in the same page update, rather
    than leaving the user looking at the tab they dragged *from*.

    Always branches into a view of your own first if this page isn't already one of your own -- see
    `save_page`. A panel/chart structure edit is never "just viewing", unlike `persist_settings`'s
    two opt-outs, so there's no equivalent flag here.
    """
    curr_page = BasicExperimentPage.model_validate_json(page_json)
    updates: dict[str, Any] = {"panels": mutate(curr_page.panels)}
    if extra_settings:
        updates["page_settings"] = {**curr_page.page_settings, **extra_settings}
    mutated = curr_page.model_copy(update=updates)
    store = get_data_store()
    saved = save_page(store, mutated)
    return saved, accordion_view(store, saved)


def upsert_chart(
    panel: models.PanelInstance[Any, Any],
    panel_name: str,
    index: int | None,
    chart: models.ChartInstance[Any, Any],
) -> models.PanelInstance[Any, Any]:
    """Add `chart` to `panel` (if index is None) or replace the chart at `index`."""
    if panel.name != panel_name:
        return panel
    if index is None:
        return panel.model_copy(update={"charts": [*panel.charts, chart]})
    new_charts = list(panel.charts)
    new_charts[index] = chart
    return panel.model_copy(update={"charts": new_charts})


def add_chart_to_panel_by_name(
    panels: list[models.PanelInstance[Any, Any]], panel_name: str, chart: models.ChartInstance[Any, Any]
) -> list[models.PanelInstance[Any, Any]]:
    """Append `chart` to the panel named `panel_name`, creating that panel if it doesn't exist yet."""
    for i, p in enumerate(panels):
        if p.name == panel_name:
            new_panels = list(panels)
            new_panels[i] = p.model_copy(update={"charts": [*p.charts, chart]})
            return new_panels
    return [*panels, models.PanelInstance(name=panel_name, charts=[chart])]


def reorder_panel(
    panels: list[models.PanelInstance[Any, Any]], panel_name: str, target_name: str, *, after: bool
) -> list[models.PanelInstance[Any, Any]]:
    """Move the panel named `panel_name` to sit just before/after the panel named `target_name`."""
    if panel_name == target_name:
        return panels
    moving = next((p for p in panels if p.name == panel_name), None)
    if moving is None or not any(p.name == target_name for p in panels):
        return panels
    remaining = [p for p in panels if p.name != panel_name]
    target_idx = next(i for i, p in enumerate(remaining) if p.name == target_name)
    remaining.insert(target_idx + 1 if after else target_idx, moving)
    return remaining


def set_panel_sync(
    panels: list[models.PanelInstance[Any, Any]], panel_name: str, *, sync: bool
) -> list[models.PanelInstance[Any, Any]]:
    return [p.model_copy(update={"sync": sync}) if p.name == panel_name else p for p in panels]


def update_panel(
    panels: list[models.PanelInstance[Any, Any]], panel_name: str, changes: dict[str, Any]
) -> list[models.PanelInstance[Any, Any]]:
    """Apply `changes` (already-validated `PanelInstance` field values) to the panel named `panel_name`."""
    return [p.model_copy(update=changes) if p.name == panel_name else p for p in panels]


def move_index[T](items: list[T], index: int, target_index: int, *, after: bool) -> list[T]:
    """Move the item at `index` to sit just before/after `target_index`; a no-op if either is out of range."""
    if index == target_index or not (0 <= index < len(items)) or not (0 <= target_index < len(items)):
        return items
    items = list(items)
    item = items.pop(index)
    # `target_index` is the target's index in the *original* list -- popping `item` out from before
    # it shifts it left by one, so account for that before adding the requested offset.
    insert_at = target_index + (1 if after else 0) - (1 if index < target_index else 0)
    items.insert(max(0, min(insert_at, len(items))), item)
    return items


def reorder_chart(
    panels: list[models.PanelInstance[Any, Any]],
    panel_name: str,
    index: int,
    target_index: int,
    *,
    after: bool,
) -> list[models.PanelInstance[Any, Any]]:
    """Move the chart at `index` in the panel named `panel_name` to sit just before/after `target_index`."""
    new_panels: list[models.PanelInstance[Any, Any]] = []
    for p in panels:
        if p.name != panel_name:
            new_panels.append(p)
            continue
        new_panels.append(
            p.model_copy(update={"charts": move_index(p.charts, index, target_index, after=after)})
        )
    return new_panels


def panel_name_taken(
    panels: list[models.PanelInstance[Any, Any]], name: str, *, ignore: str | None = None
) -> bool:
    """Whether `name` is already used by a panel other than `ignore` (the panel being renamed, if any)."""
    return any(p.name == name for p in panels if p.name != ignore)


def unique_panel_name(panels: list[models.PanelInstance[Any, Any]], base: str) -> str:
    """`base`, or `"{base} (2)"`, `"{base} (3)"`, ... -- whichever isn't already a panel name."""
    if not panel_name_taken(panels, base):
        return base
    n = 2
    while panel_name_taken(panels, f"{base} ({n})"):
        n += 1
    return f"{base} ({n})"


def _pop_chart(
    panels: list[models.PanelInstance[Any, Any]], panel_name: str, index: int
) -> tuple[models.ChartInstance[Any, Any] | None, list[models.PanelInstance[Any, Any]]]:
    """
    Remove the chart at `index` in `panel_name`.

    Returns `(None, panels)` unchanged if `panel_name`/`index` don't resolve to a real chart --
    shared by every "move this chart somewhere else" mutation (`move_chart_to_tab`,
    `move_chart_to_panel`) so each only has to say *where* the chart lands, not how to detach it.
    """
    source = next((p for p in panels if p.name == panel_name), None)
    if source is None or not (0 <= index < len(source.charts)):
        return None, panels
    chart = source.charts[index]
    without_chart = [
        p.model_copy(update={"charts": [c for i, c in enumerate(p.charts) if i != index]})
        if p.name == panel_name
        else p
        for p in panels
    ]
    return chart, without_chart


def move_chart_to_tab(
    panels: list[models.PanelInstance[Any, Any]], panel_name: str, index: int, tab: str
) -> list[models.PanelInstance[Any, Any]]:
    """
    Move the chart at `index` in `panel_name` into `tab`'s first panel, creating one if `tab` is empty.

    Unlike `reorder_chart` (same panel, different position), this can change which panel a chart
    belongs to -- and so, unlike a pure reorder, always goes through a full `mutate_panels_and_rerender`
    at the call site rather than a client-side splice, since the destination panel may not even be
    open (or may not exist yet).
    """
    chart, without_chart = _pop_chart(panels, panel_name, index)
    if chart is None:
        return panels
    dest = next((p for p in without_chart if p.tab == tab), None)
    if dest is not None:
        return [
            p.model_copy(update={"charts": [*p.charts, chart]}) if p.name == dest.name else p
            for p in without_chart
        ]
    new_panel = models.PanelInstance(
        name=unique_panel_name(without_chart, tab or "General"), tab=tab, charts=[chart]
    )
    return [*without_chart, new_panel]


def move_chart_to_panel(  # noqa: PLR0913
    panels: list[models.PanelInstance[Any, Any]],
    panel_name: str,
    index: int,
    target_panel: str,
    target_index: int | None,
    *,
    after: bool,
) -> list[models.PanelInstance[Any, Any]]:
    """
    Move the chart at `index` in `panel_name` into `target_panel`, at `target_index` there.

    `target_index=None` appends instead of inserting at a position -- the drop landed on the target
    panel's body generally (an empty panel, or anywhere that isn't one of its existing charts)
    rather than on a specific chart to land next to. `panel_name == target_panel` degrades to a plain
    `reorder_chart` (same-panel move, client-splice-friendly) rather than repeating that logic here.
    """
    if panel_name == target_panel:
        return (
            panels
            if target_index is None
            else reorder_chart(panels, panel_name, index, target_index, after=after)
        )
    chart, without_chart = _pop_chart(panels, panel_name, index)
    if chart is None or not any(p.name == target_panel for p in without_chart):
        return panels

    def _insert(p: models.PanelInstance[Any, Any]) -> models.PanelInstance[Any, Any]:
        if p.name != target_panel:
            return p
        if target_index is None:
            return p.model_copy(update={"charts": [*p.charts, chart]})
        new_charts = list(p.charts)
        insert_at = max(0, min(target_index + (1 if after else 0), len(new_charts)))
        new_charts.insert(insert_at, chart)
        return p.model_copy(update={"charts": new_charts})

    return [_insert(p) for p in without_chart]


def _find_accordion_containing(node: Any, item_value: str) -> dict[str, Any] | None:  # noqa: ANN401
    """
    Depth-first search a wire-format Dash component tree for the `Accordion` holding `item_value`.

    Panels grouped under the same tab share one `Accordion` (see `BasicExperimentPage.render`), and
    with more than one tab, each sits inside its own `TabsPanel` rather than at a fixed position in
    the tree -- searching for the `AccordionItem` actually being dragged is what lets
    `reorder_rendered_panels`/`reorder_rendered_charts` stay agnostic to whether `Tabs` are in play.
    """
    if isinstance(node, list):
        for item in cast("list[Any]", node):
            found = _find_accordion_containing(item, item_value)
            if found is not None:
                return found
        return None
    if not isinstance(node, dict) or "props" not in node:
        return None
    node = cast("dict[str, Any]", node)
    props = cast("dict[str, Any]", node["props"])
    children = cast("list[Any]", props.get("children") or [])
    if node.get("type") == "Accordion" and any(
        isinstance(c, dict) and cast("dict[str, Any]", c).get("props", {}).get("value") == item_value
        for c in children
    ):
        return node
    return _find_accordion_containing(children, item_value)


def reorder_rendered_panels(container: dict[str, Any], order: list[str]) -> dict[str, Any]:
    """
    Reorder an already-rendered `accordion_view` container's panels in place, by panel name.

    A panel drag changes only display order, never a chart's content -- redoing the (potentially
    expensive) per-panel dataframe fetch and chart render for every open panel on every drag, just
    to end up with the exact same charts in a different order, is pure waste. `container` is the
    client's own cached copy of `METRIC_CONTENT_ID.children` (an already-rendered `accordion_view`
    tree, serialized to plain dicts by Dash), so this just splices the dragged panel's own
    `Accordion`'s children -- each an `AccordionItem` keyed by its `value` (the panel name) -- into
    the requested order.
    """
    accordion = _find_accordion_containing(container, order[0])
    if accordion is None:
        msg = f"no accordion contains panel {order[0]!r}"
        raise ValueError(msg)
    items = accordion["props"]["children"]
    by_name = {item["props"]["value"]: item for item in items}
    accordion["props"]["children"] = [by_name[name] for name in order]
    return container


def reorder_rendered_charts(container: dict[str, Any], panel_name: str, order: list[int]) -> dict[str, Any]:
    """
    Reorder one already-rendered panel's charts in place, by their original index. See `reorder_rendered_panels`.

    `order` is a permutation of the panel's original chart indices (from `move_index` applied to
    `range(len(panel.charts))`), not new chart data -- a chart drag never changes any chart's
    content either, just where it sits within its panel.
    """
    accordion = _find_accordion_containing(container, panel_name)
    if accordion is None:
        msg = f"no accordion contains panel {panel_name!r}"
        raise ValueError(msg)
    item = next(i for i in accordion["props"]["children"] if i["props"]["value"] == panel_name)
    accordion_panel = item["props"]["children"][1]
    panel_body = accordion_panel["props"]["children"]  # html.Div(id=panel_content_id(...))
    chart_container = panel_body["props"]["children"]  # dmc.SimpleGrid or dmc.Flex of chart items
    chart_items = chart_container["props"]["children"]
    chart_container["props"]["children"] = [chart_items[i] for i in order]
    return container


def merge_chart_param_values(
    values: list[Any],
    checked_values: list[Any],
    field_ids: list[dict[str, str]],
    fields: dict[str, models.ParameterField],
) -> dict[str, Any]:
    """
    NumberInput/TextInput/Select report via `value`; Switch reports via `checked`. Merge them by field id.

    A cleared NumberInput reports `None`, which isn't a valid `int`/`float` -- for a field with its
    own default (`required=False`), that value is dropped entirely so validation falls back to the
    field's default instead of failing with "not a valid integer".
    """
    merged = [v if v is not None else c for v, c in zip(values, checked_values, strict=True)]
    parameters: dict[str, Any] = {}
    for fid, val in zip(field_ids, merged, strict=True):
        field_name = fid["field"]
        field = fields.get(field_name)
        if val is None and field is not None and not field.required:
            continue
        parameters[field_name] = val
    return parameters


def build_validated_chart_instance(
    chart_type_name: str, parameters: dict[str, Any]
) -> models.ChartInstance[Any, Any]:
    """
    Build a `models.ChartInstance`, validating `parameters` against the chart type's own settings model.

    `models.ChartInstance.parameters` is an untyped `dict[str, object]` -- constructing one directly never
    raises even for garbage values (e.g. a cleared numeric field), so callers that need to surface a
    validation error to the user (rather than have it surface later, unguarded, from
    `hint_required_columns`/`render`) must validate through here instead.
    """
    chart_type = models.ChartTypeRegistry.get_chart_type(chart_type_name)
    chart_type.parameter_type().model_validate(parameters)
    return models.ChartInstance[Any, Any](chart_type=chart_type_name, parameters=parameters)


class EditCtx(TypedDict):
    """
    Base for the grouped ("flexible callback signature") `State` dicts panel/chart mutations take.

    One dict argument instead of several separate ones keeps those callbacks under ruff's
    `max-args`; each subclass adds whatever that one callback needs next to `page_json`.
    """

    page_json: str


def register_state_callbacks(app: Dash) -> None:
    """Callbacks belonging to the core render tree itself, not any one feature."""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input({"type": "panel-accordion", "tab": ALL}, "value"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def persist_open_panel(open_values: list[list[str] | None], experiment_id: int, page_json: str) -> str:
        open_value = [name for group in open_values for name in (group or [])]
        curr_page = BasicExperimentPage.model_validate_json(page_json)
        # Mirrors `toggle_panel_layout`'s own "a mount can echo the current value back as a
        # 'change'" check: the accordion's `value` prop mounting with whatever `accordion_view`
        # just initialized it to (every render, including ones that aren't about panels being open
        # at all) reports back here as if the user had just opened/closed something. Comparing
        # against what's already stored catches that mount echo before it turns into a write --
        # which matters beyond just the wasted round trip: unlike a real open/close, that write's
        # own `page_json` can be arbitrarily stale (captured whenever the accordion was last built,
        # not necessarily this instant), and writing it back can undo a *later* edit that branched
        # into a view of its own in between.
        stored_open = curr_page.page_settings.get(
            OPEN_PANEL_KEY, [curr_page.panels[0].name] if curr_page.panels else []
        )
        normalized_stored = [stored_open] if isinstance(stored_open, str) else stored_open
        if open_value == normalized_stored:
            raise PreventUpdate
        store = get_data_store()
        # Which panel is expanded is per-viewer browsing state, not part of what the view "is" --
        # see `persist_settings`'s own docstring on why this one opts out of branching. Its ref
        # comes from the accordion's own page (`ref_of`), not `STATE_VIEW_ID` -- the accordion
        # remounting is itself often a *side effect* of a structural edit elsewhere on this same
        # page that just branched into a new view, and `STATE_VIEW_ID` only catches up to that a
        # separate round trip later. Reading it here would race that catch-up and, since this
        # write goes straight through regardless of ownership, land back on the view the edit had
        # already branched away from.
        page = persist_settings(
            store, ref_of(experiment_id, curr_page), {OPEN_PANEL_KEY: open_value}, branch_on_edit=False
        )
        return page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output({"type": "panel-content", "panel": ALL}, "children"),
        Output(LOADED_PANELS_STORE_ID, "data"),
        Input({"type": "panel-accordion", "tab": ALL}, "value"),
        State({"type": "panel-content", "panel": ALL}, "id"),
        State(LOADED_PANELS_STORE_ID, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def render_opened_panels(
        open_values: list[list[str] | None],
        panel_ids: list[dict[str, str]],
        loaded: list[str] | None,
        experiment_id: int,
        page_json: str,
    ) -> tuple[list[Any], list[str]]:
        open_set = {name for group in open_values for name in (group or [])}
        loaded_set = set(loaded or [])
        newly_opened = open_set - loaded_set
        if not newly_opened:
            raise PreventUpdate

        store = get_data_store()
        # Same `ref_of`-over-`STATE_VIEW_ID` reasoning as `persist_open_panel` -- this accordion
        # remount can itself be the tail end of an edit that just branched into a new view.
        curr_page = BasicExperimentPage.model_validate_json(page_json)
        page = load_page(store, ref_of(experiment_id, curr_page))
        panel_by_name = {p.name: p for p in page.panels}

        outputs: list[Any] = []
        for pid in panel_ids:
            name = pid["panel"]
            if name not in newly_opened or name not in panel_by_name:
                outputs.append(no_update)
                continue
            outputs.append(
                render_panel_content(store, experiment_id, panel_by_name[name], page.page_settings)
            )
        return outputs, sorted(loaded_set | newly_opened)

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(PANEL_TABS_ID, "value"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def persist_active_tab(tab_value: str | None, experiment_id: int, page_json: str) -> str:
        if tab_value is None:
            raise PreventUpdate
        curr_page = BasicExperimentPage.model_validate_json(page_json)
        # Same mount-echo guard as `persist_open_panel`, and for the same reason: `PANEL_TABS_ID`
        # mounting with whichever tab `accordion_view` already had active reports back here as if
        # the user had just switched tabs.
        if curr_page.page_settings.get(ACTIVE_TAB_KEY) == tab_from_component_value(tab_value):
            raise PreventUpdate
        store = get_data_store()
        # Which tab is active is per-viewer browsing state -- same reasoning, including the ref,
        # as `persist_open_panel`.
        page = persist_settings(
            store,
            ref_of(experiment_id, curr_page),
            {ACTIVE_TAB_KEY: tab_from_component_value(tab_value)},
            branch_on_edit=False,
        )
        return page.model_dump_json()

    # Tab switching is entirely client-side (Mantine's own state, no server round trip) -- so
    # whether "Rename the active tab" should be disabled (on the ungrouped "General" tab) has to
    # update client-side too, not just from this render's initial `disabled=`.
    app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        _ACTIVE_TAB_DISABLES_RENAME_JS.source,
        Output(RENAME_TAB_BUTTON_ID, "disabled"),
        Input(PANEL_TABS_ID, "value"),
    )

    # Both pure client-side, deliberately never round-tripping to the server -- see
    # `live_switch_state.js`/`live_last_fetch_label.js` and the constants' own docstrings above.
    app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        _LIVE_SWITCH_STATE_JS.source,
        Output(LIVE_POLL_INTERVAL_ID, "disabled"),
        Output(LIVE_STATUS_ID, "style"),
        Output(LIVE_PAUSED_BADGE_ID, "style"),
        Input(LIVE_UPDATES_ENABLED_ID, "checked"),
    )
    app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        _LIVE_LAST_FETCH_LABEL_JS.source,
        Output(LIVE_LAST_FETCH_LABEL_ID, "children"),
        Input(LIVE_TICK_INTERVAL_ID, "n_intervals"),
        State(STATE_LAST_FETCH_AT, "data"),
    )
