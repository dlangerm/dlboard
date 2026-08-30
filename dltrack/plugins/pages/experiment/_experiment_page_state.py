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

import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict, cast

import dash_mantine_components as dmc
import pandas as pd
from dash import ALL, Dash, Input, Output, State, ctx, html, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from structlog.stdlib import get_logger

from dltrack import models
from dltrack.models import ButtonId, DivId, ModalId, StoreId, ValueId, constants
from dltrack.plugins.pages.experiment import _dataframe_helpers as dfh
from dltrack.serve import get_data_store

if TYPE_CHECKING:
    from collections.abc import Callable

    from dash.development.base_component import Component

    from dltrack.models import DataStore

_log = get_logger(__name__)


class ExperimentPage:
    """Page tag: marks a component id as belonging to the basic-experiment-page plugin."""


# --- route-skeleton ids: a two-file contract with `serve/_pages/experiment.py`, which imports
# these from `simple_experiment_page.py` (this module's public façade) rather than from here. ---
PAGE_EXPERIMENT_ID: DivId[ExperimentPage] = DivId("experiment-container")
EXPERIMENT_HEADER_ID: DivId[ExperimentPage] = DivId("experiment-header")
EXPERIMENT_HEADER_ACTIONS_ID: DivId[ExperimentPage] = DivId("experiment-header-actions")
METRIC_CONTENT_ID: DivId[ExperimentPage] = DivId("metrics-view")
STATE_HPARAMS: StoreId[ExperimentPage] = StoreId("hparams-state")
STATE_PAGE_STORAGE: StoreId[ExperimentPage] = StoreId("current-page")

# --- accordion / panel state ---
ACCORDION_ID: ValueId[ExperimentPage] = ValueId("experiment-accordion")
OPEN_PANEL_KEY: typing.Final = "open_panel"  # a page_settings dict key, not a component id

FULL_DF_STORE_ID: StoreId[ExperimentPage] = StoreId("full-dataframe-store")
COLUMN_KINDS_STORE_ID: StoreId[ExperimentPage] = StoreId("column-kinds-store")
LOADED_PANELS_STORE_ID: StoreId[ExperimentPage] = StoreId("loaded-panels-store")

NEW_PANEL_ID: ButtonId[ExperimentPage] = ButtonId("new-panel-button")
NEW_PANEL_NAME_ID: ValueId[ExperimentPage] = ValueId("panel-name")

PACKED_GRID_COLS: typing.Final = 3
"""Fixed column count for a panel's `"grid"` layout -- not user-configurable, matching the
"packed" default's goal of not needing per-panel tuning."""

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

AUTO_POPULATE_DELIMITER_ID: ValueId[ExperimentPage] = ValueId("auto-populate-delimiter")
AUTO_POPULATE_MODE_ID: ValueId[ExperimentPage] = ValueId("auto-populate-mode")
AUTO_POPULATE_BUTTON_ID: ButtonId[ExperimentPage] = ButtonId("auto-populate-button")

_SPLIT_MODE_DATA = [{"label": "Prefix", "value": "prefix"}, {"label": "Suffix", "value": "suffix"}]
DEFAULT_DELIMITER: typing.Final = "/"

# --- rename-panel modal shell (callbacks in `_panel_controls.py`) ---
RENAME_PANEL_MODAL_ID: ModalId[ExperimentPage] = ModalId("rename-panel-modal")
RENAME_PANEL_TARGET_ID: StoreId[ExperimentPage] = StoreId("rename-panel-target")
RENAME_PANEL_NAME_INPUT_ID: ValueId[ExperimentPage] = ValueId("rename-panel-name-input")
RENAME_PANEL_ERROR_ID: DivId[ExperimentPage] = DivId("rename-panel-error")
RENAME_PANEL_SAVE_ID: ButtonId[ExperimentPage] = ButtonId("rename-panel-save")
RENAME_PANEL_CANCEL_ID: ButtonId[ExperimentPage] = ButtonId("rename-panel-cancel")

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


PanelMoveDirection = typing.Literal["up", "down"]
ChartMoveDirection = typing.Literal["left", "right"]


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


def delete_chart_button_id(panel_name: str, index: int) -> ChartID:
    return {"type": "delete-chart", "panel": panel_name, "index": index}


def move_chart_button_id(panel_name: str, index: int, direction: ChartMoveDirection) -> dict[str, str | int]:
    return {"type": "move-chart", "panel": panel_name, "index": index, "direction": direction}


def delete_panel_button_id(panel_name: str) -> dict[str, str]:
    return {"type": "delete-panel", "panel": panel_name}


def rename_panel_button_id(panel_name: str) -> dict[str, str]:
    return {"type": "rename-panel", "panel": panel_name}


def panel_sync_switch_id(panel_name: str) -> dict[str, str]:
    return {"type": "panel-sync", "panel": panel_name}


def panel_layout_control_id(panel_name: str) -> dict[str, str]:
    return {"type": "panel-layout", "panel": panel_name}


def move_panel_button_id(panel_name: str, direction: PanelMoveDirection) -> dict[str, str]:
    return {"type": "move-panel", "panel": panel_name, "direction": direction}


def chart_controls_group_id(panel_name: str, index: int) -> ChartID:
    return {"type": "chart-controls", "panel": panel_name, "index": index}


def panel_content_id(panel_name: str) -> dict[str, str]:
    return {"type": "panel-content", "panel": panel_name}


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

    metrics_df = pd.DataFrame()
    if metric_cols is None:
        _log.warning("A panel had a missing hint, fetching every metric, this can be expensive")
        metrics_df = dfh.build_metrics_dataframe(store.fetch_metrics(experiment_id))
    elif metric_cols:
        metrics_df = dfh.build_metrics_dataframe(
            store.fetch_metrics(experiment_id=experiment_id, metric_name_match=metric_cols)
        )

    artifacts_df = pd.DataFrame()
    if artifact_keys:
        artifacts_df = dfh.build_artifacts_dataframe(
            store.fetch_artifacts(
                experiment_id=experiment_id, keys={k for k in artifact_keys if k is not None}
            )
        )

    hparam_keys = panel.hint_required_hparams()
    hparams_df = pd.DataFrame()
    if hparam_keys is None:
        # fetch_hyperparams has no server-side key filter (unlike fetch_metrics), so "fetch
        # everything" and "fetch a specific set" cost the same query -- only the local column
        # filter in dfh.build_hyperparams_dataframe differs.
        hparams_df = dfh.build_hyperparams_dataframe(store.fetch_hyperparams(experiment_id))
    elif hparam_keys:
        hparams_df = dfh.build_hyperparams_dataframe(store.fetch_hyperparams(experiment_id), keys=hparam_keys)

    df = dfh.merge_metrics_and_artifacts(metrics_df, artifacts_df)
    df = dfh.merge_hyperparams(df, hparams_df)
    return dfh.filter_excluded_runs(df, page_settings)


def compute_full_df_and_column_kinds(store: DataStore[...], experiment_id: int) -> tuple[str, dict[str, str]]:
    """
    Fetch every metric/artifact/hparam for `experiment_id` and infer each column's kind.

    The expensive "load everything" path -- used wherever a column-kind lookup is needed but
    `FULL_DF_STORE_ID`/`COLUMN_KINDS_STORE_ID` haven't been populated yet by an add/edit-chart or
    suggest-charts interaction this page load, e.g. auto-generating charts right after opening a
    brand new experiment.
    """
    metrics_df = dfh.build_metrics_dataframe(store.fetch_metrics(experiment_id))
    artifacts = list(store.fetch_artifacts(experiment_id=experiment_id))
    artifacts_df = dfh.build_artifacts_dataframe(artifacts)
    hparams = list(store.fetch_hyperparams(experiment_id))
    hparams_df = dfh.build_hyperparams_dataframe(hparams)
    df = dfh.merge_metrics_and_artifacts(metrics_df, artifacts_df)
    df = dfh.merge_hyperparams(df, hparams_df)
    hparam_keys = {k for h in hparams for k in h.hparams_dict}
    column_kinds = dfh.infer_column_kinds(metrics_df.columns, {a.key for a in artifacts}, hparam_keys)
    return df.to_json(orient="split", date_format="iso"), {k: v.value for k, v in column_kinds.items()}


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


def render_panel_charts(panel: models.PanelInstance[Any, Any], dataframe: pd.DataFrame) -> list[dmc.Stack]:
    """Render each chart in a panel with edit/delete controls above it, revealed on hover."""
    items: list[dmc.Stack] = []
    last_index = len(panel.charts) - 1
    for idx, chart in enumerate(panel.charts):
        rendered = _apply_panel_sync(
            _render_chart_safely(chart, dataframe, panel.name), panel_name=panel.name, sync=panel.sync
        )
        items.append(
            dmc.Stack(
                [
                    dmc.Group(
                        [
                            dmc.ActionIcon(
                                "←",
                                id=move_chart_button_id(panel.name, idx, "left"),
                                n_clicks=0,
                                disabled=idx == 0,
                                variant="subtle",
                                size="xs",
                            ),
                            dmc.ActionIcon(
                                "→",
                                id=move_chart_button_id(panel.name, idx, "right"),
                                n_clicks=0,
                                disabled=idx == last_index,
                                variant="subtle",
                                size="xs",
                            ),
                            dmc.ActionIcon(
                                "✎",
                                id=edit_chart_button_id(panel.name, idx),  # pyright: ignore[reportArgumentType]
                                n_clicks=0,
                                variant="subtle",
                                size="xs",
                            ),
                            dmc.ActionIcon(
                                "🗑",
                                id=delete_chart_button_id(panel.name, idx),  # pyright: ignore[reportArgumentType]
                                n_clicks=0,
                                variant="subtle",
                                color="red",
                                size="xs",
                            ),
                        ],
                        id=chart_controls_group_id(panel.name, idx),  # pyright: ignore[reportArgumentType]
                        justify="flex-end",
                        gap="xs",
                        className="dl-chart-controls",
                    ),
                    rendered,
                ],
                gap="xs",
                w="100%" if panel.layout == "grid" else chart.natural_width(),
                maw="100%",
                p="xs",
                className="dl-chart-item",
            )
        )
    return items


def render_panel_content(
    store: DataStore[...],
    experiment_id: int,
    panel: models.PanelInstance[Any, Any],
    page_settings: dict[str, Any],
) -> list[Component]:
    """Fetch + render one panel's charts. Only called for panels that are actually open."""
    df = fetch_panel_dataframe(store, experiment_id, panel, page_settings)
    items = render_panel_charts(panel, df)
    container = (
        dmc.SimpleGrid(items, cols=PACKED_GRID_COLS, spacing="lg")
        if panel.layout == "grid"
        else dmc.Flex(items, justify="flex-start", gap="lg", wrap="wrap")
    )
    return [
        container,
        dmc.Button(
            id=open_chart_button_id(panel.name),
            n_clicks=0,
            children="+",
            className="dl-add-chart-btn",
        ),
    ]


def _panel_placeholder() -> dmc.Skeleton:
    return dmc.Skeleton(height=60, radius="sm")


def panel_header_controls(panel: models.PanelInstance[Any, Any], *, index: int, count: int) -> Component:
    """Sync/layout/rename/move/delete for one panel, hover-revealed on the panel's own header."""
    panel_name = panel.name
    return dmc.Group(
        [
            dmc.Switch(
                id=panel_sync_switch_id(panel_name),
                label="Sync",
                checked=panel.sync,
                size="xs",
            ),
            dmc.SegmentedControl(
                id=panel_layout_control_id(panel_name),
                data=[
                    {"value": "packed", "label": "Packed"},
                    {"value": "grid", "label": "Grid"},
                ],
                value=panel.layout,
                size="xs",
            ),
            dmc.ActionIcon(
                "✎",
                id=rename_panel_button_id(panel_name),
                n_clicks=0,
                variant="subtle",
                size="sm",
            ),
            dmc.ActionIcon(
                "↑",
                id=move_panel_button_id(panel_name, "up"),
                n_clicks=0,
                disabled=index == 0,
                variant="subtle",
                size="sm",
            ),
            dmc.ActionIcon(
                "↓",
                id=move_panel_button_id(panel_name, "down"),
                n_clicks=0,
                disabled=index == count - 1,
                variant="subtle",
                size="sm",
            ),
            dmc.ActionIcon(
                "🗑",
                id=delete_panel_button_id(panel_name),
                n_clicks=0,
                variant="subtle",
                color="red",
                size="sm",
            ),
        ],
        className="dl-panel-controls",
        gap="xs",
        wrap="nowrap",
    )


def panel_header(panel: models.PanelInstance[Any, Any], *, index: int, count: int) -> Component:
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
            panel_header_controls(panel, index=index, count=count),
        ],
        className="dl-panel-item-header",
        gap=0,
        wrap="nowrap",
    )


# ============================================================
# page model
# ============================================================


class BasicExperimentPage(models.Page[pd.DataFrame, dmc.Accordion, html.Div], frozen=True, extra="forbid"):
    """Basic experiment page: per-panel charts rendered in an accordion."""

    @typing.override
    def render(self, data_store: DataStore[...], experiment_id: int) -> dmc.Accordion:
        """Render the accordion. Only open panels get real content; closed ones get a placeholder."""
        open_value = self.page_settings.get(OPEN_PANEL_KEY, [self.panels[0].name] if self.panels else [])
        if isinstance(open_value, str):
            open_value = [open_value]
        open_set = set(open_value)  # pyright: ignore[reportUnknownVariableType, reportArgumentType]
        count = len(self.panels)

        return dmc.Accordion(
            id=ACCORDION_ID,
            multiple=True,
            value=open_value,  # pyright: ignore[reportArgumentType]
            variant="contained",
            chevronPosition="left",
            children=[
                dmc.AccordionItem(
                    [
                        panel_header(p, index=idx, count=count),
                        dmc.AccordionPanel(
                            html.Div(
                                id=panel_content_id(p.name),
                                className="dl-panel-body",
                                children=(
                                    render_panel_content(data_store, experiment_id, p, self.page_settings)
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
                for idx, p in enumerate(self.panels)
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


def empty_view_helper(panels: list[models.PanelInstance[Any, Any]]) -> Component:
    """Auto-populate charts for a brand-new (empty) view; suggest un-charted keys once it isn't."""
    add_panel = dmc.Group(
        [
            dmc.TextInput(id=NEW_PANEL_NAME_ID, placeholder="New Panel Name"),
            dmc.Button(id=NEW_PANEL_ID, n_clicks=0, children="Create"),
        ],
        gap=0,
    )
    if not panels:
        return dmc.Group(
            [
                add_panel,
                dmc.TextInput(
                    id=AUTO_POPULATE_DELIMITER_ID,
                    label="Delimiter",
                    value=DEFAULT_DELIMITER,
                    w=90,
                    size="sm",
                ),
                split_mode_control(AUTO_POPULATE_MODE_ID),
                dmc.Button(
                    "Auto-generate charts",
                    id=AUTO_POPULATE_BUTTON_ID,
                    n_clicks=0,
                    variant="light",
                    size="sm",
                ),
            ],
            align="flex-end",
            gap="sm",
        )
    return dmc.Group(
        [
            add_panel,
            dmc.Button("Suggest charts", id=SUGGEST_CHARTS_BUTTON_ID, n_clicks=0, variant="light", size="sm"),
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


@dataclass(frozen=True, slots=True)
class EditViewState:
    """
    Client-cached dataframe/column-kind cache carried across a panel/chart mutation and its re-render.

    Dash reports these as two separate `dcc.Store` properties, so callbacks still receive them as
    two positional arguments -- but every callback bundles them into one `EditViewState`
    immediately, so `accordion_view` and the mutation helpers below only need to thread one value
    through instead of two.
    """

    full_df_json: str | None = None
    column_kinds: dict[str, str] | None = None


def accordion_view(
    store: DataStore[...],
    experiment_id: int,
    *,
    view_state: EditViewState | None = None,
) -> html.Div:
    """
    Accordion view for experiments.

    `view_state` lets callers that re-render the accordion mid-edit (adding a panel/chart,
    changing run selection, etc.) carry the cached dataframe forward instead of silently
    resetting it.
    """
    view_state = view_state or EditViewState()
    _log.debug("rendering chart for experiment %s", experiment_id)
    page = cast(
        "BasicExperimentPage", store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    )
    open_value = page.page_settings.get(OPEN_PANEL_KEY, [page.panels[0].name] if page.panels else [])
    if isinstance(open_value, str):
        open_value = [open_value]

    return html.Div(
        [
            dmc.Stack(
                [
                    empty_view_helper(page.panels),
                    page.render(store, experiment_id),
                ],
                gap="xs",
            ),
            Store(id=LOADED_PANELS_STORE_ID, data=list(open_value)),  # pyright: ignore[reportArgumentType]
            Store(id=FULL_DF_STORE_ID, data=view_state.full_df_json),
            Store(id=COLUMN_KINDS_STORE_ID, data=view_state.column_kinds),
            _add_chart_modal(),
            _suggest_charts_drawer(),
            _rename_panel_modal(),
            _delete_panel_confirm_modal(),
            _delete_chart_confirm_modal(),
        ],
    )


# ============================================================
# page-mutation primitives (shared by every controller module)
# ============================================================


def persist_settings(
    store: DataStore[...], experiment_id: int, updates: dict[str, Any]
) -> BasicExperimentPage:
    """Merge `updates` into page_settings (server-authoritative) and persist -- no accordion rebuild."""
    page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    new_settings = {**page.page_settings, **updates}
    page = page.model_copy(update={"page_settings": new_settings})
    return cast("BasicExperimentPage", store.update_page(page))


def persist_settings_and_rerender(
    store: DataStore[...],
    experiment_id: int,
    updates: dict[str, Any],
    *,
    view_state: EditViewState | None = None,
) -> tuple[BasicExperimentPage, dmc.Container]:
    """Merge `updates` into page_settings (server-authoritative), persist, and re-render the accordion."""
    page = persist_settings(store, experiment_id, updates)
    container = accordion_view(store, experiment_id=experiment_id, view_state=view_state)
    return page, container  # pyright: ignore[reportReturnType]


def mutate_panels_and_rerender(
    page_json: str,
    experiment_id: int,
    mutate: Callable[[list[models.PanelInstance[Any, Any]]], list[models.PanelInstance[Any, Any]]],
    *,
    view_state: EditViewState | None = None,
) -> tuple[BasicExperimentPage, html.Div]:
    """Load page from client-cached state, apply `mutate` to its panels, persist, and re-render."""
    curr_page = BasicExperimentPage.model_validate_json(page_json)
    curr_page = curr_page.model_copy(update={"panels": mutate(curr_page.panels)})
    store = get_data_store()
    curr_page = store.update_page(curr_page)
    container = accordion_view(store, experiment_id=experiment_id, view_state=view_state)
    return curr_page, container  # pyright: ignore[reportReturnType]


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


def move_panel(
    panels: list[models.PanelInstance[Any, Any]], panel_name: str, direction: PanelMoveDirection
) -> list[models.PanelInstance[Any, Any]]:
    """Swap the panel named `panel_name` with its neighbor in `direction`."""
    idx = next((i for i, p in enumerate(panels) if p.name == panel_name), None)
    if idx is None:
        return panels
    swap_with = idx - 1 if direction == "up" else idx + 1
    if swap_with < 0 or swap_with >= len(panels):
        return panels
    new_panels = list(panels)
    new_panels[idx], new_panels[swap_with] = new_panels[swap_with], new_panels[idx]
    return new_panels


def set_panel_sync(
    panels: list[models.PanelInstance[Any, Any]], panel_name: str, *, sync: bool
) -> list[models.PanelInstance[Any, Any]]:
    return [p.model_copy(update={"sync": sync}) if p.name == panel_name else p for p in panels]


def set_panel_layout(
    panels: list[models.PanelInstance[Any, Any]], panel_name: str, layout: typing.Literal["packed", "grid"]
) -> list[models.PanelInstance[Any, Any]]:
    return [p.model_copy(update={"layout": layout}) if p.name == panel_name else p for p in panels]


def move_chart(
    panels: list[models.PanelInstance[Any, Any]], panel_name: str, index: int, direction: ChartMoveDirection
) -> list[models.PanelInstance[Any, Any]]:
    """Swap the chart at `index` in the panel named `panel_name` with its neighbor in `direction`."""
    swap_with = index - 1 if direction == "left" else index + 1
    new_panels: list[models.PanelInstance[Any, Any]] = []
    for p in panels:
        if p.name != panel_name or swap_with < 0 or swap_with >= len(p.charts):
            new_panels.append(p)
            continue
        charts = list(p.charts)
        charts[index], charts[swap_with] = charts[swap_with], charts[index]
        new_panels.append(p.model_copy(update={"charts": charts}))
    return new_panels


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


def edit_ctx_state() -> dict[str, Any]:
    """
    Grouped `State` for the `(page_json, full_df_json, column_kinds)` trio most mutation callbacks need.

    A Dash "flexible callback signature" `State` argument instead of three separate ones -- this is
    what keeps `create_panel`/`add_chart`/`delete_chart`/etc. under ruff's `max-args`.
    `experiment_id` deliberately isn't part of this group: most of these callbacks read it as a
    `State`, but `create_panel` listens to it as an `Input` too, so it stays a separate,
    per-callback dependency instead of being folded in here.
    """
    return {
        "page_json": models.store_state(STATE_PAGE_STORAGE),
        "view_state": {
            "full_df_json": models.store_state(FULL_DF_STORE_ID, allow_optional=True),
            "column_kinds": models.store_state(COLUMN_KINDS_STORE_ID, allow_optional=True),
        },
    }


class EditCtx(TypedDict):
    page_json: str
    view_state: dict[str, Any]


def edit_view_state_from_ctx(edit_ctx: EditCtx) -> EditViewState:
    return EditViewState(
        full_df_json=edit_ctx["view_state"]["full_df_json"],
        column_kinds=edit_ctx["view_state"]["column_kinds"],
    )


def register_state_callbacks(app: Dash) -> None:
    """Callbacks belonging to the core render tree itself, not any one feature."""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(ACCORDION_ID, "value"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def persist_open_panel(open_value: str | list[str] | None, experiment_id: int) -> str:
        store = get_data_store()
        page = persist_settings(store, experiment_id, {OPEN_PANEL_KEY: open_value or []})
        return page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output({"type": "panel-content", "panel": ALL}, "children"),
        Output(LOADED_PANELS_STORE_ID, "data"),
        Input(ACCORDION_ID, "value"),
        State({"type": "panel-content", "panel": ALL}, "id"),
        State(LOADED_PANELS_STORE_ID, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def render_opened_panels(
        open_value: list[str] | None,
        panel_ids: list[dict[str, str]],
        loaded: list[str] | None,
        experiment_id: int,
    ) -> tuple[list[Any], list[str]]:
        open_set = set(open_value or [])
        loaded_set = set(loaded or [])
        newly_opened = open_set - loaded_set
        if not newly_opened:
            raise PreventUpdate

        store = get_data_store()
        page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
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
