"""The basic experiment page: hyperparameter table + accordion of metric/artifact charts."""

from __future__ import annotations

import itertools
import typing
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

import dash_mantine_components as dmc
import pandas as pd
from dash import ALL, MATCH, Dash, Input, NoUpdate, Output, State, ctx, dash_table, html, no_update
from dash.dash_table.Format import Format
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from flask import Response
from pydantic import ValidationError
from structlog.stdlib import get_logger

from dltrack.models import (
    ChartInstance,
    ChartTypeRegistry,
    ColumnKind,
    ExperimentSource,
    HyperParams,
    Page,
    PanelInstance,
    ParameterField,
    ParameterFieldType,
    Run,
    constants,
)
from dltrack.plugins.charts._table_style import NUMERIC, infer_column_dtype, themed_datatable_kwargs
from dltrack.plugins.pages._chart_autogen import (
    Suggestion,
    build_auto_panels,
    build_suggestions,
    find_uncharted_keys,
)
from dltrack.plugins.pages._dataframe_helpers import (
    build_artifacts_dataframe,
    build_hyperparams_dataframe,
    build_metrics_dataframe,
    experiment_display_name,
    filter_excluded_runs,
    group_columns_by_kind,
    infer_column_kinds,
    merge_hyperparams,
    merge_metrics_and_artifacts,
)
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
from dltrack.serve import ClientsideScript, get_current_user, get_data_store

if TYPE_CHECKING:
    from collections.abc import Callable

    from dash.development.base_component import Component

    from dltrack.models import DataStore
    from dltrack.plugins.pages._chart_autogen import SplitMode

_log = get_logger(__name__)


NEW_PANEL_ID = "new-panel-button"
NEW_PANEL_NAME_ID = "panel-name"
ACCORDION_ID = "experiment-accordion"
OPEN_PANEL_KEY: typing.Final = "open_panel"

FULL_DF_STORE_ID = "full-dataframe-store"
COLUMN_KINDS_STORE_ID = "column-kinds-store"

ADD_CHART_MODAL_ID = "add-chart-modal"
LOADED_PANELS_STORE_ID = "loaded-panels-store"

PACKED_GRID_COLS: typing.Final = 3
"""Fixed column count for a panel's `"grid"` layout -- not user-configurable, matching the
"packed" default's goal of not needing per-panel tuning."""

_CHART_PARAM_CLEAR_JS = ClientsideScript(Path(__file__).with_name("chart_param_clear.js"))
_HPARAM_COLS_CHANGED_JS = ClientsideScript(Path(__file__).with_name("hparam_cols_changed.js"))

_HOVER_CSS_PATH = Path(__file__).with_name("_experiment_page_hover.css")
_HOVER_CSS_ROUTE = "experiment-page-hover.css"


class _ChartTargetData(TypedDict):
    panel: str
    index: int | None


class _ChartID(_ChartTargetData):
    type: str


_PanelMoveDirection = typing.Literal["up", "down"]


@dataclass(frozen=True, slots=True)
class EditViewState:
    """
    Client-cached dataframe/column-kind cache carried across a panel/chart mutation and its re-render.

    Dash reports these as two separate `dcc.Store` properties, so callbacks still receive them as
    two positional arguments — but every callback bundles them into one `EditViewState`
    immediately, so `accordion_view` and the mutation helpers below only need to thread one value
    through instead of two.
    """

    full_df_json: str | None = None
    column_kinds: dict[str, str] | None = None


ADD_CHART_TARGET_ID = "add-chart-target"
ADD_CHART_INITIAL_PARAMS_ID = "add-chart-initial-params"
ADD_CHART_TYPE_SELECT_ID = "add-chart-type-select"
ADD_CHART_PARAMS_ID = "add-chart-params"
ADD_CHART_PREVIEW_ID = "add-chart-preview"
ADD_CHART_ERROR_ID = "add-chart-error"
ADD_CHART_SUBMIT_ID = "add-chart-submit"
ADD_CHART_CANCEL_ID = "add-chart-cancel"

_SPLIT_MODE_DATA = [{"label": "Prefix", "value": "prefix"}, {"label": "Suffix", "value": "suffix"}]
_DEFAULT_DELIMITER = "/"

AUTO_POPULATE_DELIMITER_ID = "auto-populate-delimiter"
AUTO_POPULATE_MODE_ID = "auto-populate-mode"
AUTO_POPULATE_BUTTON_ID = "auto-populate-button"

SUGGEST_CHARTS_BUTTON_ID = "suggest-charts-button"
SUGGEST_DRAWER_ID = "suggest-charts-drawer"
SUGGEST_DELIMITER_ID = "suggest-charts-delimiter"
SUGGEST_MODE_ID = "suggest-charts-mode"
SUGGEST_CONTENT_ID = "suggest-charts-content"
SUGGEST_SUGGESTIONS_STORE_ID = "suggest-charts-store"

RENAME_PANEL_MODAL_ID = "rename-panel-modal"
RENAME_PANEL_TARGET_ID = "rename-panel-target"
RENAME_PANEL_NAME_INPUT_ID = "rename-panel-name-input"
RENAME_PANEL_ERROR_ID = "rename-panel-error"
RENAME_PANEL_SAVE_ID = "rename-panel-save"
RENAME_PANEL_CANCEL_ID = "rename-panel-cancel"

DELETE_PANEL_MODAL_ID = "delete-panel-modal"
DELETE_PANEL_TARGET_ID = "delete-panel-target"
DELETE_PANEL_CONFIRM_ID = "delete-panel-confirm"
DELETE_PANEL_CANCEL_ID = "delete-panel-cancel"

DELETE_CHART_MODAL_ID = "delete-chart-modal"
DELETE_CHART_TARGET_ID = "delete-chart-target"
DELETE_CHART_CONFIRM_ID = "delete-chart-confirm"
DELETE_CHART_CANCEL_ID = "delete-chart-cancel"

EXPERIMENT_DESC_IDS = DescriptionEditorIds(
    header=constants.EXPERIMENT_HEADER_ID,
    edit_button="experiment-edit-desc-button",
    modal="experiment-edit-desc-modal",
    textarea="experiment-edit-desc-textarea",
    save="experiment-edit-desc-save",
    cancel="experiment-edit-desc-cancel",
)

EXPERIMENT_DELETE_IDS = DeleteConfirmIds(
    button=constants.DELETE_EXPERIMENT_BUTTON_ID,
    modal=constants.DELETE_EXPERIMENT_MODAL_ID,
    confirm=constants.DELETE_EXPERIMENT_CONFIRM_ID,
    cancel=constants.DELETE_EXPERIMENT_CANCEL_ID,
)

NAVBAR_HPARAM_DATATABLE_ID = "navbar-hparam-datatable"
NAVBAR_HPARAM_COL_SELECT_ID = "navbar-hparam-col-select"
NAVBAR_HPARAM_TABLE_BODY_ID = "navbar-hparam-table-body"
NAVBAR_HPARAM_CONFIRM_COLS_ID = "navbar-hparam-confirm-cols"
NAVBAR_HPARAM_APPLIED_COLS_ID = "navbar-hparam-applied-cols"
_NAVBAR_HPARAM_PAGE_SIZE = 8
_DELETE_COLUMN_ID = "_delete"

_METRIC_BOOKKEEPING_COLS = {"run_id", "step", "index", "timestamp_utc", "experiment_id"}


# ============================================================
# id helpers
# ============================================================


def _open_chart_button_id(panel_name: str) -> dict[str, str]:
    return {"type": "open-add-chart", "panel": panel_name}


def _edit_chart_button_id(panel_name: str, index: int) -> _ChartID:
    return {"type": "edit-chart", "panel": panel_name, "index": index}


def _delete_chart_button_id(panel_name: str, index: int) -> _ChartID:
    return {"type": "delete-chart", "panel": panel_name, "index": index}


_ChartMoveDirection = typing.Literal["left", "right"]


def _move_chart_button_id(
    panel_name: str, index: int, direction: _ChartMoveDirection
) -> dict[str, str | int]:
    return {"type": "move-chart", "panel": panel_name, "index": index, "direction": direction}


def _delete_panel_button_id(panel_name: str) -> dict[str, str]:
    return {"type": "delete-panel", "panel": panel_name}


def _rename_panel_button_id(panel_name: str) -> dict[str, str]:
    return {"type": "rename-panel", "panel": panel_name}


def _panel_sync_switch_id(panel_name: str) -> dict[str, str]:
    return {"type": "panel-sync", "panel": panel_name}


def _panel_layout_control_id(panel_name: str) -> dict[str, str]:
    return {"type": "panel-layout", "panel": panel_name}


def _move_panel_button_id(panel_name: str, direction: _PanelMoveDirection) -> dict[str, str]:
    return {"type": "move-panel", "panel": panel_name, "direction": direction}


def _chart_controls_group_id(panel_name: str, index: int) -> _ChartID:
    return {"type": "chart-controls", "panel": panel_name, "index": index}


def _chart_param_id(field_name: str) -> dict[str, str]:
    return {"type": "chart-param", "field": field_name}


def _chart_param_clear_id(field_name: str) -> dict[str, str]:
    return {"type": "chart-param-clear", "field": field_name}


def _panel_content_id(panel_name: str) -> dict[str, str]:
    return {"type": "panel-content", "panel": panel_name}


def _add_suggestion_button_id(kind: str, key: str) -> dict[str, str]:
    return {"type": "add-suggestion", "kind": kind, "key": key}


def _fetch_panel_dataframe(
    store: DataStore[...], experiment_id: int, panel: PanelInstance[Any, Any], page_settings: dict[str, Any]
) -> pd.DataFrame:
    """Fetch just the data one panel needs — not the whole experiment."""
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
        metrics_df = build_metrics_dataframe(store.fetch_metrics(experiment_id))
    elif metric_cols:
        metrics_df = build_metrics_dataframe(
            store.fetch_metrics(experiment_id=experiment_id, metric_name_match=metric_cols)
        )

    artifacts_df = pd.DataFrame()
    if artifact_keys:
        artifacts_df = build_artifacts_dataframe(
            store.fetch_artifacts(
                experiment_id=experiment_id, keys={k for k in artifact_keys if k is not None}
            )
        )

    hparam_keys = panel.hint_required_hparams()
    hparams_df = pd.DataFrame()
    if hparam_keys is None:
        # fetch_hyperparams has no server-side key filter (unlike fetch_metrics), so "fetch
        # everything" and "fetch a specific set" cost the same query — only the local column
        # filter in build_hyperparams_dataframe differs.
        hparams_df = build_hyperparams_dataframe(store.fetch_hyperparams(experiment_id))
    elif hparam_keys:
        hparams_df = build_hyperparams_dataframe(store.fetch_hyperparams(experiment_id), keys=hparam_keys)

    df = merge_metrics_and_artifacts(metrics_df, artifacts_df)
    df = merge_hyperparams(df, hparams_df)
    return filter_excluded_runs(df, page_settings)


def _compute_full_df_and_column_kinds(
    store: DataStore[...], experiment_id: int
) -> tuple[str, dict[str, str]]:
    """
    Fetch every metric/artifact/hparam for `experiment_id` and infer each column's kind.

    The expensive "load everything" path -- used wherever a column-kind lookup is needed but
    `FULL_DF_STORE_ID`/`COLUMN_KINDS_STORE_ID` haven't been populated yet by an add/edit-chart or
    suggest-charts interaction this page load, e.g. auto-generating charts right after opening a
    brand new experiment.
    """
    metrics_df = build_metrics_dataframe(store.fetch_metrics(experiment_id))
    artifacts = list(store.fetch_artifacts(experiment_id=experiment_id))
    artifacts_df = build_artifacts_dataframe(artifacts)
    hparams = list(store.fetch_hyperparams(experiment_id))
    hparams_df = build_hyperparams_dataframe(hparams)
    df = merge_metrics_and_artifacts(metrics_df, artifacts_df)
    df = merge_hyperparams(df, hparams_df)
    hparam_keys = {k for h in hparams for k in h.hparams_dict}
    column_kinds = infer_column_kinds(metrics_df.columns, {a.key for a in artifacts}, hparam_keys)
    return df.to_json(orient="split", date_format="iso"), {k: v.value for k, v in column_kinds.items()}


def _render_panel_content(
    store: DataStore[...],
    experiment_id: int,
    panel: PanelInstance[Any, Any],
    page_settings: dict[str, Any],
) -> list[Component]:
    """Fetch + render one panel's charts. Only called for panels that are actually open."""
    df = _fetch_panel_dataframe(store, experiment_id, panel, page_settings)
    items = _render_panel_charts(panel, df)
    container = (
        dmc.SimpleGrid(items, cols=PACKED_GRID_COLS, spacing="lg")
        if panel.layout == "grid"
        else dmc.Flex(items, justify="flex-start", gap="lg", wrap="wrap")
    )
    return [
        container,
        dmc.Button(
            id=_open_chart_button_id(panel.name),
            n_clicks=0,
            children="+",
            className="dl-add-chart-btn",
        ),
    ]


def _panel_placeholder() -> dmc.Skeleton:
    return dmc.Skeleton(height=60, radius="sm")


def _panel_header_controls(panel: PanelInstance[Any, Any], *, index: int, count: int) -> Component:
    """
    Sync/layout/rename/move/delete for one panel, hover-revealed on the panel's own header.

    Reuses the existing pattern-matched ids and callbacks (`_panel_sync_switch_id`,
    `_panel_layout_control_id`, `_rename_panel_button_id`, `_move_panel_button_id`,
    `_delete_panel_button_id` / `toggle_panel_sync`, `toggle_panel_layout`,
    `open_rename_panel_modal`, `move_panel`, `delete_panel`) -- only *where* these render moves, the
    state-mutation logic behind them is untouched. Settings first, destructive action last.
    """
    panel_name = panel.name
    return dmc.Group(
        [
            dmc.Switch(
                id=_panel_sync_switch_id(panel_name),
                label="Sync",
                checked=panel.sync,
                size="xs",
            ),
            dmc.SegmentedControl(
                id=_panel_layout_control_id(panel_name),
                data=[
                    {"value": "packed", "label": "Packed"},
                    {"value": "grid", "label": "Grid"},
                ],
                value=panel.layout,
                size="xs",
            ),
            dmc.ActionIcon(
                "✎",
                id=_rename_panel_button_id(panel_name),
                n_clicks=0,
                variant="subtle",
                size="sm",
            ),
            dmc.ActionIcon(
                "↑",
                id=_move_panel_button_id(panel_name, "up"),
                n_clicks=0,
                disabled=index == 0,
                variant="subtle",
                size="sm",
            ),
            dmc.ActionIcon(
                "↓",
                id=_move_panel_button_id(panel_name, "down"),
                n_clicks=0,
                disabled=index == count - 1,
                variant="subtle",
                size="sm",
            ),
            dmc.ActionIcon(
                "🗑",
                id=_delete_panel_button_id(panel_name),
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


def _panel_header(panel: PanelInstance[Any, Any], *, index: int, count: int) -> Component:
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
            _panel_header_controls(panel, index=index, count=count),
        ],
        className="dl-panel-item-header",
        gap=0,
        wrap="nowrap",
    )


# ============================================================
# page model
# ============================================================


class BasicExperimentPage(Page[pd.DataFrame, dmc.Accordion, html.Div], frozen=True, extra="forbid"):
    """Basic experiment page: per-panel charts rendered in an accordion."""

    @typing.override
    def retrieve_dataframes(self, store: DataStore[...], experiment_id: int) -> list[pd.DataFrame]:
        return [_fetch_panel_dataframe(store, experiment_id, p, self.page_settings) for p in self.panels]

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
                        _panel_header(p, index=idx, count=count),
                        dmc.AccordionPanel(
                            html.Div(
                                id=_panel_content_id(p.name),
                                className="dl-panel-body",
                                children=(
                                    _render_panel_content(data_store, experiment_id, p, self.page_settings)
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


def _render_chart_safely(
    chart: ChartInstance[Any, Any], dataframe: pd.DataFrame, panel_name: str
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


def _render_panel_charts(panel: PanelInstance[Any, Any], dataframe: pd.DataFrame) -> list[dmc.Stack]:
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
                                id=_move_chart_button_id(panel.name, idx, "left"),
                                n_clicks=0,
                                disabled=idx == 0,
                                variant="subtle",
                                size="xs",
                            ),
                            dmc.ActionIcon(
                                "→",
                                id=_move_chart_button_id(panel.name, idx, "right"),
                                n_clicks=0,
                                disabled=idx == last_index,
                                variant="subtle",
                                size="xs",
                            ),
                            dmc.ActionIcon(
                                "✎",
                                id=_edit_chart_button_id(panel.name, idx),  # pyright: ignore[reportArgumentType]
                                n_clicks=0,
                                variant="subtle",
                                size="xs",
                            ),
                            dmc.ActionIcon(
                                "🗑",
                                id=_delete_chart_button_id(panel.name, idx),  # pyright: ignore[reportArgumentType]
                                n_clicks=0,
                                variant="subtle",
                                color="red",
                                size="xs",
                            ),
                        ],
                        id=_chart_controls_group_id(panel.name, idx),  # pyright: ignore[reportArgumentType]
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


def _is_lightning_experiment(data_store: DataStore[...], experiment_id: int) -> bool:
    """
    Whether `experiment_id`'s metrics were logged through `DLTrackLogger`.

    Lightning-specific chart-autogen conventions (see `_chart_autogen.lightning_granularity`) only
    apply to those.
    """
    experiment = data_store.get_experiment(experiment_id)
    return experiment is not None and experiment.source == ExperimentSource.PYTORCH_LIGHTNING


def _split_mode_control(control_id: str) -> dmc.SegmentedControl:
    return dmc.SegmentedControl(id=control_id, data=_SPLIT_MODE_DATA, value="prefix", size="sm")


def _empty_view_helper(panels: list[PanelInstance[Any, Any]]) -> Component:
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
                    value=_DEFAULT_DELIMITER,
                    w=90,
                    size="sm",
                ),
                _split_mode_control(AUTO_POPULATE_MODE_ID),
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


def _render_suggestions(suggestions: list[Suggestion]) -> Component:
    if not suggestions:
        return dmc.Text("Every metric and artifact already has a chart.", c="dimmed", size="sm")

    def _row(s: Suggestion) -> Component:
        return dmc.Group(
            [
                dmc.Stack(
                    [
                        dmc.Text(s.key, size="sm", ff="monospace"),
                        dmc.Text(f"→ {s.panel_name}", size="xs", c="dimmed"),
                    ],
                    gap=0,
                ),
                dmc.ActionIcon(
                    "+",
                    id=_add_suggestion_button_id(s.kind.value, s.key),
                    n_clicks=0,
                    variant="light",
                    size="sm",
                ),
            ],
            justify="space-between",
            wrap="nowrap",
        )

    metrics = [s for s in suggestions if s.kind == ColumnKind.METRIC]
    artifacts = [s for s in suggestions if s.kind == ColumnKind.ARTIFACT]
    sections: list[Component] = []
    if metrics:
        sections.append(dmc.Text("Metrics", fw=600, size="sm", mt="sm"))
        sections.extend(_row(s) for s in metrics)
    if artifacts:
        sections.append(dmc.Text("Artifacts", fw=600, size="sm", mt="sm"))
        sections.extend(_row(s) for s in artifacts)
    return dmc.Stack(sections, gap="xs")


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
                        id=SUGGEST_DELIMITER_ID, label="Delimiter", value=_DEFAULT_DELIMITER, w=90, size="sm"
                    ),
                    _split_mode_control(SUGGEST_MODE_ID),
                ],
                gap="sm",
                mb="sm",
            ),
            html.Div(id=SUGGEST_CONTENT_ID),
        ],
    )


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
                    _empty_view_helper(page.panels),
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
# page-mutation helpers (shared by multiple callbacks below)
# ============================================================


def _persist_settings(
    store: DataStore[...], experiment_id: int, updates: dict[str, Any]
) -> BasicExperimentPage:
    """Merge `updates` into page_settings (server-authoritative) and persist -- no accordion rebuild."""
    page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    new_settings = {**page.page_settings, **updates}
    page = page.model_copy(update={"page_settings": new_settings})
    return cast("BasicExperimentPage", store.update_page(page))


def _persist_settings_and_rerender(
    store: DataStore[...],
    experiment_id: int,
    updates: dict[str, Any],
    *,
    view_state: EditViewState | None = None,
) -> tuple[BasicExperimentPage, dmc.Container]:
    """Merge `updates` into page_settings (server-authoritative), persist, and re-render the accordion."""
    page = _persist_settings(store, experiment_id, updates)
    container = accordion_view(store, experiment_id=experiment_id, view_state=view_state)
    return page, container  # pyright: ignore[reportReturnType]


def _mutate_panels_and_rerender(
    page_json: str,
    experiment_id: int,
    mutate: Callable[[list[PanelInstance[Any, Any]]], list[PanelInstance[Any, Any]]],
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


def _upsert_chart(
    panel: PanelInstance[Any, Any], panel_name: str, index: int | None, chart: ChartInstance[Any, Any]
) -> PanelInstance[Any, Any]:
    """Add `chart` to `panel` (if index is None) or replace the chart at `index`."""
    if panel.name != panel_name:
        return panel
    if index is None:
        return panel.model_copy(update={"charts": [*panel.charts, chart]})
    new_charts = list(panel.charts)
    new_charts[index] = chart
    return panel.model_copy(update={"charts": new_charts})


def _add_chart_to_panel_by_name(
    panels: list[PanelInstance[Any, Any]], panel_name: str, chart: ChartInstance[Any, Any]
) -> list[PanelInstance[Any, Any]]:
    """Append `chart` to the panel named `panel_name`, creating that panel if it doesn't exist yet."""
    for i, p in enumerate(panels):
        if p.name == panel_name:
            new_panels = list(panels)
            new_panels[i] = p.model_copy(update={"charts": [*p.charts, chart]})
            return new_panels
    return [*panels, PanelInstance(name=panel_name, charts=[chart])]


def _move_panel(
    panels: list[PanelInstance[Any, Any]], panel_name: str, direction: _PanelMoveDirection
) -> list[PanelInstance[Any, Any]]:
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


def _set_panel_sync(
    panels: list[PanelInstance[Any, Any]], panel_name: str, *, sync: bool
) -> list[PanelInstance[Any, Any]]:
    return [p.model_copy(update={"sync": sync}) if p.name == panel_name else p for p in panels]


def _set_panel_layout(
    panels: list[PanelInstance[Any, Any]], panel_name: str, layout: typing.Literal["packed", "grid"]
) -> list[PanelInstance[Any, Any]]:
    return [p.model_copy(update={"layout": layout}) if p.name == panel_name else p for p in panels]


def _move_chart(
    panels: list[PanelInstance[Any, Any]], panel_name: str, index: int, direction: _ChartMoveDirection
) -> list[PanelInstance[Any, Any]]:
    """Swap the chart at `index` in the panel named `panel_name` with its neighbor in `direction`."""
    swap_with = index - 1 if direction == "left" else index + 1
    new_panels: list[PanelInstance[Any, Any]] = []
    for p in panels:
        if p.name != panel_name or swap_with < 0 or swap_with >= len(p.charts):
            new_panels.append(p)
            continue
        charts = list(p.charts)
        charts[index], charts[swap_with] = charts[swap_with], charts[index]
        new_panels.append(p.model_copy(update={"charts": charts}))
    return new_panels


def _merge_chart_param_values(
    values: list[Any],
    checked_values: list[Any],
    field_ids: list[dict[str, str]],
    fields: dict[str, ParameterField],
) -> dict[str, Any]:
    """
    NumberInput/TextInput/Select report via `value`; Switch reports via `checked`. Merge them by field id.

    A cleared NumberInput reports `None`, which isn't a valid `int`/`float` — for a field with its
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


def _build_validated_chart_instance(
    chart_type_name: str, parameters: dict[str, Any]
) -> ChartInstance[Any, Any]:
    """
    Build a `ChartInstance`, validating `parameters` against the chart type's own settings model.

    `ChartInstance.parameters` is an untyped `dict[str, object]` -- constructing one directly never
    raises even for garbage values (e.g. a cleared numeric field), so callers that need to surface a
    validation error to the user (rather than have it surface later, unguarded, from
    `hint_required_columns`/`render`) must validate through here instead.
    """
    chart_type = ChartTypeRegistry.get_chart_type(chart_type_name)
    chart_type.parameter_type().model_validate(parameters)
    return ChartInstance[Any, Any](chart_type=chart_type_name, parameters=parameters)


# ============================================================
# add/edit-chart modal
# ============================================================


def _param_field_input(
    field_name: str,
    field: ParameterField,
    columns_by_kind: dict[ColumnKind, list[str]],
    *,
    override: bool | int | float | str | list[str] | None = None,
) -> Component:
    input_id = _chart_param_id(field_name)
    label = f"{field_name} *" if field.required else field_name
    value = override if override is not None else field.default

    match field.type:
        case ParameterFieldType.BOOL:
            return dmc.Switch(
                id=input_id,
                label=label,
                checked=bool(value) if value is not None else False,
                description=field.description,
            )
        case ParameterFieldType.INT | ParameterFieldType.FLOAT:
            number_value = cast("int | float | None", value)
            return dmc.NumberInput(
                id=input_id,
                label=label,
                value=number_value,
                step=1 if field.type == ParameterFieldType.INT else 0.1,
                description=field.description,
                # Optional fields (a default, not required) get a visible clear button that resets
                # to that default -- clearing via backspace alone works too, but isn't discoverable.
                rightSection=(
                    dmc.ActionIcon(
                        "✕",
                        id=_chart_param_clear_id(field_name),
                        n_clicks=0,
                        variant="subtle",
                        color="gray",
                        size="xs",
                    )
                    if not field.required
                    else None
                ),
                rightSectionPointerEvents="all",
            )
        case ParameterFieldType.STR | ParameterFieldType.LIST_STR:
            if field.choices is not None:
                select_value = cast("str | None", value)
                return dmc.Select(
                    id=input_id,
                    label=label,
                    data=list(field.choices),
                    value=select_value,
                    allowDeselect=False,
                    description=field.description,
                )

            options = columns_by_kind.get(field.column_kind, []) if field.column_kind is not None else None
            if field.type == ParameterFieldType.LIST_STR:
                multiselect_value = cast("list[str] | None", value)
                return dmc.MultiSelect(
                    id=input_id,
                    label=label,
                    data=sorted(options) if options else [],
                    value=multiselect_value or [],
                    searchable=True,
                    description=field.description,
                )
            if options:
                return dmc.Select(
                    id=input_id,
                    label=label,
                    data=sorted(options),
                    value=value,  # pyright: ignore[reportArgumentType]
                    searchable=True,
                    description=field.description,
                )
            return dmc.TextInput(
                id=input_id,
                label=label,
                value=value or "",  # pyright: ignore[reportArgumentType]
                description=field.description,
            )


def _add_chart_modal() -> dmc.Modal:
    chart_types = sorted(ChartTypeRegistry.get_registered_chart_types().keys())
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


# ============================================================
# hparam / metrics table
# ============================================================


def _fetch_last_step_metrics(
    store: DataStore[...], experiment_id: int, metric_name_match: set[str]
) -> dict[int, dict[str, Any]]:
    """One row per run: `metric_name_match`'s values at that run's highest logged step."""
    if not metric_name_match:
        return {}
    df = build_metrics_dataframe(store.fetch_metrics(experiment_id, metric_name_match=metric_name_match))
    if df.empty:
        return {}
    last_rows = df.loc[df.groupby("run_id")["step"].idxmax()]
    metric_cols = [c for c in df.columns if c not in _METRIC_BOOKKEEPING_COLS]
    return {
        int(row["run_id"]): {c: row[c] for c in metric_cols if pd.notna(row[c])}
        for _, row in last_rows.iterrows()
    }


def _build_hparam_rows(
    runs: list[Run], hparams_by_run: dict[int, HyperParams], last_step_metrics: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """One row per run, whether or not it has logged hyperparameters or metrics yet."""
    rows: list[dict[str, Any]] = []
    for run in runs:
        row: dict[str, Any] = {"run_id": run.id, "run_name": run.name or f"Run {run.id}"}
        hparam = hparams_by_run.get(run.id)
        if hparam is not None:
            row.update(hparam.hparams_dict)
        row.update(last_step_metrics.get(run.id, {}))
        rows.append(row)
    return rows


_HPARAM_ROWS_LIMIT = 1000
"""Cap on runs loaded into the hparam/metric comparison table -- a page, not every row, matching
the discipline every other `list_*` `DataStore` method already follows."""


def _load_hparam_view_data(
    store: DataStore[...], experiment_id: int, hparams: list[str], selected_metrics: set[str]
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """
    Load every run for the experiment, enriched with its hparams and any selected last-step metrics.

    Metric *names* come from a cheap `DISTINCT key` lookup (for the Columns picker); metric
    *values* are only fetched for `selected_metrics` -- the columns the table is actually about to
    show. Fetching every metric ever logged in the experiment just to populate a table nobody's
    added a metric column to yet is the difference between this loading instantly and taking
    seconds once an experiment has any real volume of logged steps.
    """
    hydrated = [HyperParams.model_validate_json(run) for run in hparams]
    hparams_by_run = {h.run_id: h for h in hydrated}
    hparam_keys = sorted(set(itertools.chain(*[list(k.hparams_dict.keys()) for k in hydrated])))
    metric_keys = store.list_metric_keys(experiment_id)
    last_step_metrics = _fetch_last_step_metrics(store, experiment_id, selected_metrics & set(metric_keys))
    runs = list(store.get_runs(experiment_id, limit=_HPARAM_ROWS_LIMIT, offset=0))
    rows = _build_hparam_rows(runs, hparams_by_run, last_step_metrics)
    return hparam_keys, metric_keys, rows


def _delete_run_modal() -> Component:
    """
    Confirmation modal for deleting a run.

    Opened by clicking a row's trash icon in the table (see `_DELETE_COLUMN_ID` in
    `_build_hparam_datatable`) -- mirrors the project/experiment delete modals `render_delete_control`
    builds, but doesn't use that helper directly since its confirm target here
    (`DELETE_RUN_PENDING_STORE_ID`) is resolved per-row rather than from a fixed State.
    """
    return dmc.Modal(
        id=constants.DELETE_RUN_MODAL_ID,
        title="Delete this run?",
        opened=False,
        children=[
            dmc.Text(
                "This soft-deletes the run and everything under it. It can be restored from the "
                "admin Trash until it's permanently purged."
            ),
            dmc.Group(
                [
                    dmc.Button("Cancel", id=constants.DELETE_RUN_CANCEL_ID, variant="default"),
                    dmc.Button("Delete", id=constants.DELETE_RUN_CONFIRM_ID, color="red"),
                ],
                justify="flex-end",
                mt="sm",
            ),
        ],
    )


def _build_hparam_datatable(
    rows: list[dict[str, Any]],
    selected: list[str] | list[int],
    excluded: list[int] | list[str],
    *,
    table_id: str,
    page_size: int,
) -> dash_table.DataTable:
    columns: list[dict[str, Any]] = [{"name": "Run", "id": "run_name"}]
    for key in selected:
        col: dict[str, Any] = {"name": key, "id": key}
        if infer_column_dtype(rows, str(key)) == NUMERIC:
            col["type"] = "numeric"
            col["format"] = Format(precision=3, scheme="s")
        columns.append(col)
    columns.append({"name": "", "id": _DELETE_COLUMN_ID, "clearable": False})

    selected_rows = [i for i, row in enumerate(rows) if row["run_id"] not in excluded]
    data = [{**row, _DELETE_COLUMN_ID: "🗑"} for row in rows]

    return dash_table.DataTable(
        id=table_id,
        columns=columns,  # pyright: ignore[reportArgumentType]
        data=data,  # pyright: ignore[reportArgumentType]
        row_selectable="multi",
        selected_rows=selected_rows,
        filter_action="native",
        sort_action="native",
        page_action="native",
        page_size=page_size,
        style_cell_conditional=[
            {
                "if": {"column_id": _DELETE_COLUMN_ID},
                "width": "28px",
                "maxWidth": "28px",
                "textAlign": "center",
                "cursor": "pointer",
                "color": "var(--mantine-color-red-6)",
            }
        ],
        **themed_datatable_kwargs(),
    )


def _run_summary(rows: list[dict[str, Any]], excluded: list[int] | list[str]) -> Component:
    """`N runs` / `M excluded` text, shown above the navbar's run comparison table."""
    text = f"{len(rows)} runs" + (f" · {len(excluded)} excluded" if excluded else "")
    return dmc.Text(text, size="xs", c="dimmed")


def _render_hparam_panel(
    rows: list[dict[str, Any]],
    hparam_keys: list[str],
    metric_keys: list[str],
    selected: list[str],
    excluded: list[int] | list[str],
) -> Component:
    """
    The Columns picker + comparison table, always visible in the navbar.

    Column changes only take effect on "Apply" (not per-tick) -- computing the table involves a
    hyperparameter/metric fetch per run, so committing it once per intended change instead of once
    per checkbox click keeps a multi-column edit from queueing up a burst of redundant store reads.
    "Apply" itself only appears once the picked columns actually differ from what's applied (a
    clientside callback compares against `NAVBAR_HPARAM_APPLIED_COLS_ID`'s baseline), and the row
    it sits in never wraps (`wrap="nowrap"`), so it appearing/disappearing can't push the table
    below it up or down.
    """
    applied = [k for k in selected if k in hparam_keys or k in metric_keys]
    return html.Div(
        [
            dmc.Group(
                [
                    dmc.MultiSelect(
                        id=NAVBAR_HPARAM_COL_SELECT_ID,
                        label="Columns",
                        data=[
                            {"group": "Hyperparameters", "items": hparam_keys},
                            {"group": "Metrics (last step)", "items": metric_keys},
                        ],
                        value=applied,
                        searchable=True,
                        clearable=True,
                        size="xs",
                        style={"flex": 1, "minWidth": 0},
                    ),
                    dmc.Button(
                        "Apply",
                        id=NAVBAR_HPARAM_CONFIRM_COLS_ID,
                        size="xs",
                        mt=22,
                        style={"display": "none", "flexShrink": 0},
                    ),
                    Store(id=NAVBAR_HPARAM_APPLIED_COLS_ID, data=applied),
                ],
                align="flex-end",
                gap="xs",
                wrap="nowrap",
            ),
            html.Div(
                id=NAVBAR_HPARAM_TABLE_BODY_ID,
                children=_build_hparam_datatable(
                    rows,
                    selected,
                    excluded,
                    table_id=NAVBAR_HPARAM_DATATABLE_ID,
                    page_size=_NAVBAR_HPARAM_PAGE_SIZE,
                ),
            ),
        ]
    )


def _require_triggered_id() -> Any:  # noqa: ANN401
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


# ============================================================
# callbacks
# ============================================================


def _serve_hover_css() -> Response:
    return Response(_HOVER_CSS_PATH.read_text(), mimetype="text/css")


def plug(app: Dash) -> None:  # noqa: C901, PLR0915
    """Plugin for the basic experiment page: hparam table + chart accordion + editor."""
    prefix = str(app.config.routes_pathname_prefix)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    css_route = f"{prefix}{_HOVER_CSS_ROUTE}"
    app.server.add_url_rule(css_route, endpoint=css_route, view_func=_serve_hover_css)
    app.css.append_css({"external_url": css_route, "external_only": True})

    # --- initial render: fills METRIC_CONTENT_ID/EXPERIMENT_HEADER_ID and seeds STATE_PAGE_STORAGE ---
    # Runs into `EXPERIMENT_HEADER_ACTIONS_ID` only once, at page load — that content (the Runs
    # button and Manage-panels button) is never regenerated by later callbacks.
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(constants.EXPERIMENT_HEADER_ID, "children", allow_duplicate=True),
        Output(constants.EXPERIMENT_HEADER_ACTIONS_ID, "children"),
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
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
            accordion_view(store, experiment_id=experiment_id),
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

    # --- delete a single run, via its row's trash icon -- redirects back to this same page with a
    # hard refresh (not a soft `use_pages` navigation) since the path doesn't change, and nothing
    # short of a fresh `layout()` call re-fetches `STATE_HPARAMS`/the metrics accordion.
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.DELETE_RUN_MODAL_ID, "opened", allow_duplicate=True),
        Output(constants.DELETE_RUN_PENDING_STORE_ID, "data"),
        Input(NAVBAR_HPARAM_DATATABLE_ID, "active_cell", allow_optional=True),
        State(NAVBAR_HPARAM_DATATABLE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def open_delete_run_modal(
        active_cell: dict[str, Any] | None, table_data: list[dict[str, Any]] | None
    ) -> tuple[bool, int]:
        if not active_cell or table_data is None or active_cell["column_id"] != _DELETE_COLUMN_ID:
            raise PreventUpdate
        row_index = cast("int", active_cell["row"])
        return True, int(table_data[row_index]["run_id"])

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.DELETE_RUN_MODAL_ID, "opened", allow_duplicate=True),
        Input(constants.DELETE_RUN_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_delete_run(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.LOCATION_ID, "href", allow_duplicate=True),
        Output(constants.LOCATION_ID, "refresh", allow_duplicate=True),
        Output(constants.DELETE_RUN_MODAL_ID, "opened", allow_duplicate=True),
        Input(constants.DELETE_RUN_CONFIRM_ID, "n_clicks"),
        State(constants.DELETE_RUN_PENDING_STORE_ID, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def confirm_delete_run(n_clicks: int, run_id: int | None, experiment_id: int) -> tuple[str, bool, bool]:
        if not n_clicks or run_id is None:
            raise PreventUpdate
        store = get_data_store()
        store.delete_run(run_id, get_current_user(store))
        return f"/experiment/{experiment_id}", True, False

    # The ✕ button next to an optional numeric chart-param field (see `_param_field_input`)
    # resets it to empty/default -- purely a client-side convenience for something backspace
    # already does, so no round trip needed.
    app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        _CHART_PARAM_CLEAR_JS.source,
        Output({"type": "chart-param", "field": MATCH}, "value"),
        Input({"type": "chart-param-clear", "field": MATCH}, "n_clicks"),
        prevent_initial_call=True,
    )

    # --- hparam/metric comparison table + run selection, always visible in the navbar
    # (NAVBAR_RUN_LIST_ID) ---
    def _load_hparam_panel_data(
        store: DataStore[...], experiment_id: int, hparams: list[str]
    ) -> tuple[list[str], list[str], list[dict[str, Any]], list[int] | list[str], list[str]]:
        page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
        excluded = page.page_settings.get(constants.EXCLUDED_RUNS_KEY, [])
        assert isinstance(excluded, list)
        selected_setting = page.page_settings.get(constants.SELECTED_HPARAM_COLS_KEY, [])
        assert isinstance(selected_setting, list)
        selected = [s for s in selected_setting if isinstance(s, str)]

        hparam_keys, metric_keys, rows = _load_hparam_view_data(store, experiment_id, hparams, set(selected))
        return hparam_keys, metric_keys, rows, excluded, selected

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.NAVBAR_RUN_LIST_ID, "children"),
        Input(constants.STATE_HPARAMS, "data"),
        Input(constants.STATE_EXPERIMENT_ID, "data", allow_optional=True),
        Input(constants.STATE_PAGE_STORAGE, "data", allow_optional=True),
        prevent_initial_callback=True,
    )
    def render_navbar_hparams(
        hparams: list[str], experiment_id: int | None, _page_json: str | None
    ) -> dmc.Stack:
        if experiment_id is None:
            raise PreventUpdate

        store = get_data_store()
        hparam_keys, metric_keys, rows, excluded, selected = _load_hparam_panel_data(
            store, experiment_id, hparams
        )
        return dmc.Stack(
            [
                _run_summary(rows, excluded),
                _render_hparam_panel(rows, hparam_keys, metric_keys, selected, excluded),
                _delete_run_modal(),
                Store(id=constants.DELETE_RUN_PENDING_STORE_ID),
            ],
            gap="xs",
            p="xs",
        )

    # The Apply button only makes sense once the picked columns diverge from what's applied --
    # comparing client-side (rather than round-tripping through a server callback per keystroke)
    # keeps this instant and avoids yet another spurious-rerender source.
    app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        _HPARAM_COLS_CHANGED_JS.source,
        Output(NAVBAR_HPARAM_CONFIRM_COLS_ID, "style"),
        Input(NAVBAR_HPARAM_COL_SELECT_ID, "value"),
        State(NAVBAR_HPARAM_APPLIED_COLS_ID, "data"),
        prevent_initial_call=True,
    )

    # --- page_settings-only mutations, via _persist_settings_and_rerender ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(NAVBAR_HPARAM_CONFIRM_COLS_ID, "n_clicks", allow_optional=True),
        State(NAVBAR_HPARAM_COL_SELECT_ID, "value", allow_optional=True),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def persist_selected_hparam_cols(
        n_clicks: int | None, selected: list[str] | None, experiment_id: int
    ) -> str:
        if not n_clicks:
            raise PreventUpdate
        store = get_data_store()
        page = _persist_settings(store, experiment_id, {constants.SELECTED_HPARAM_COLS_KEY: selected or []})
        return page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Input(NAVBAR_HPARAM_DATATABLE_ID, "selected_rows", allow_optional=True),
        State(NAVBAR_HPARAM_DATATABLE_ID, "data", allow_optional=True),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(FULL_DF_STORE_ID, "data", allow_optional=True),
        State(COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def sync_run_selection(
        selected_rows: list[int] | None,
        table_data: list[dict[str, Any]] | None,
        experiment_id: int,
        full_df_json: str | None,
        column_kinds: dict[str, str] | None,
    ) -> tuple[str, dmc.Container]:
        if selected_rows is None or table_data is None:
            raise PreventUpdate
        selected_ids = {table_data[i]["run_id"] for i in selected_rows}
        all_ids = {row["run_id"] for row in table_data}
        excluded = sorted(all_ids - selected_ids)

        store = get_data_store()
        # The table's `selected_rows` is *computed from* the currently-persisted `excluded` set
        # (see `_build_hparam_datatable`), so it mounting for the first time reports a "change"
        # here even though nothing the user did actually changed anything -- Dash fires this even
        # with `prevent_initial_call=True`, because that only suppresses the very first page
        # render, not a dynamically-created component (this navbar table isn't in the static
        # layout) mounting later with an already-computed value. Skip the (expensive, chart-
        # remounting) rebuild when the recomputed set matches what's already persisted.
        current_page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
        currently_excluded = current_page.page_settings.get(constants.EXCLUDED_RUNS_KEY, [])
        if excluded == currently_excluded:
            raise PreventUpdate

        page, container = _persist_settings_and_rerender(
            store,
            experiment_id,
            {constants.EXCLUDED_RUNS_KEY: excluded},
            view_state=EditViewState(
                full_df_json=full_df_json,
                column_kinds=column_kinds,
            ),
        )
        return page.model_dump_json(), container

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(ACCORDION_ID, "value"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def persist_open_panel(open_value: str | list[str] | None, experiment_id: int) -> str:
        store = get_data_store()
        page = _persist_settings(store, experiment_id, {OPEN_PANEL_KEY: open_value or []})
        return page.model_dump_json()

    # --- panel/chart mutations, via _mutate_panels_and_rerender ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(NEW_PANEL_ID, "n_clicks"),
        Input(constants.STATE_EXPERIMENT_ID, "data"),
        State(NEW_PANEL_NAME_ID, "value"),
        State(constants.STATE_PAGE_STORAGE, "data"),
        State(FULL_DF_STORE_ID, "data", allow_optional=True),
        State(COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def create_panel(  # noqa: PLR0913
        n_clicks: int,
        experiment_id: int,
        panel_name: str,
        page_json: str,
        full_df_json: str | None,
        column_kinds: dict[str, str] | None,
    ) -> tuple[html.Div, str]:
        if not n_clicks:
            raise PreventUpdate
        if not panel_name:
            msg = "Panel name cannot be empty"
            raise ValueError(msg)

        def add_panel(panels: list[PanelInstance[Any, Any]]) -> list[PanelInstance[Any, Any]]:
            return [*panels, PanelInstance(name=panel_name)]

        page, container = _mutate_panels_and_rerender(
            page_json,
            experiment_id,
            add_panel,
            view_state=EditViewState(
                full_df_json=full_df_json,
                column_kinds=column_kinds,
            ),
        )
        return container, page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(ADD_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Output(ADD_CHART_ERROR_ID, "children", allow_duplicate=True),
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(ADD_CHART_SUBMIT_ID, "n_clicks"),
        State(ADD_CHART_TYPE_SELECT_ID, "value"),
        State({"type": "chart-param", "field": ALL}, "value"),
        State({"type": "chart-param", "field": ALL}, "checked"),
        State({"type": "chart-param", "field": ALL}, "id"),
        State(ADD_CHART_TARGET_ID, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(constants.STATE_PAGE_STORAGE, "data"),
        State(FULL_DF_STORE_ID, "data", allow_optional=True),
        State(COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def add_chart(  # noqa: PLR0913
        n_clicks: int,
        chart_type_name: str | None,
        values: list[Any],
        checked_values: list[Any],
        field_ids: list[dict[str, str]],
        target: _ChartTargetData | None,
        experiment_id: int,
        page_json: str,
        full_df_json: str | None,
        column_kinds: dict[str, str] | None,
    ) -> tuple[Any, bool, str, str | NoUpdate]:
        if not n_clicks or not chart_type_name or not target:
            raise PreventUpdate

        fields = ChartTypeRegistry.get_registered_chart_types().get(chart_type_name, {})
        parameters = _merge_chart_param_values(values, checked_values, field_ids, fields)
        try:
            new_chart = _build_validated_chart_instance(chart_type_name, parameters)
        except (ValidationError, KeyError) as exc:
            return no_update, True, f"Invalid parameters: {exc}", no_update

        panel_name, index = target["panel"], target.get("index")

        def apply_chart(panels: list[PanelInstance[Any, Any]]) -> list[PanelInstance[Any, Any]]:
            return [_upsert_chart(p, panel_name, index, new_chart) for p in panels]

        page, container = _mutate_panels_and_rerender(
            page_json,
            experiment_id,
            apply_chart,
            view_state=EditViewState(
                full_df_json=full_df_json,
                column_kinds=column_kinds,
            ),
        )
        return container, False, "", page.model_dump_json()

    # --- delete chart: click opens a confirm modal, Delete in the modal does the actual mutation ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(DELETE_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Output(DELETE_CHART_TARGET_ID, "data"),
        Input({"type": "delete-chart", "panel": ALL, "index": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def open_delete_chart_modal(_n_clicks_list: list[int]) -> tuple[bool, _ChartTargetData]:
        triggered_id = cast("_ChartTargetData", _require_triggered_id())
        return True, {"panel": triggered_id["panel"], "index": triggered_id["index"]}

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(DELETE_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Input(DELETE_CHART_CONFIRM_ID, "n_clicks"),
        State(DELETE_CHART_TARGET_ID, "data"),
        State(constants.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(FULL_DF_STORE_ID, "data", allow_optional=True),
        State(COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def delete_chart(  # noqa: PLR0913
        n_clicks: int,
        target: _ChartTargetData | None,
        page_json: str,
        experiment_id: int,
        full_df_json: str | None,
        column_kinds: dict[str, str] | None,
    ) -> tuple[html.Div | NoUpdate, str | NoUpdate, bool]:
        if not n_clicks or not target:
            raise PreventUpdate
        panel_name, index = target["panel"], target["index"]

        def remove_chart(panels: list[PanelInstance[Any, Any]]) -> list[PanelInstance[Any, Any]]:
            return [
                p
                if p.name != panel_name
                else p.model_copy(update={"charts": [c for i, c in enumerate(p.charts) if i != index]})
                for p in panels
            ]

        page, container = _mutate_panels_and_rerender(
            page_json,
            experiment_id,
            remove_chart,
            view_state=EditViewState(
                full_df_json=full_df_json,
                column_kinds=column_kinds,
            ),
        )
        return container, page.model_dump_json(), False

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(DELETE_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Input(DELETE_CHART_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_delete_chart(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False

    # --- delete panel: same shape as delete chart, above ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(DELETE_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Output(DELETE_PANEL_TARGET_ID, "data"),
        Input({"type": "delete-panel", "panel": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def open_delete_panel_modal(_n_clicks_list: list[int]) -> tuple[bool, str]:
        triggered_id = cast("dict[str, str]", _require_triggered_id())
        return True, triggered_id["panel"]

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(DELETE_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Input(DELETE_PANEL_CONFIRM_ID, "n_clicks"),
        State(DELETE_PANEL_TARGET_ID, "data"),
        State(constants.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(FULL_DF_STORE_ID, "data", allow_optional=True),
        State(COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def delete_panel(  # noqa: PLR0913
        n_clicks: int,
        panel_name: str | None,
        page_json: str,
        experiment_id: int,
        full_df_json: str | None,
        column_kinds: dict[str, str] | None,
    ) -> tuple[html.Div | NoUpdate, str | NoUpdate, bool]:
        if not n_clicks or not panel_name:
            raise PreventUpdate

        def remove_panel(panels: list[PanelInstance[Any, Any]]) -> list[PanelInstance[Any, Any]]:
            return [p for p in panels if p.name != panel_name]

        page, container = _mutate_panels_and_rerender(
            page_json,
            experiment_id,
            remove_panel,
            view_state=EditViewState(
                full_df_json=full_df_json,
                column_kinds=column_kinds,
            ),
        )
        return container, page.model_dump_json(), False

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(DELETE_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Input(DELETE_PANEL_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_delete_panel(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input({"type": "move-panel", "panel": ALL, "direction": ALL}, "n_clicks"),
        State(constants.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(FULL_DF_STORE_ID, "data", allow_optional=True),
        State(COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def move_panel(
        _n_clicks_list: list[int],
        page_json: str,
        experiment_id: int,
        full_df_json: str | None,
        column_kinds: dict[str, str] | None,
    ) -> tuple[html.Div, str]:
        triggered_id = cast("dict[str, str]", _require_triggered_id())
        panel_name = triggered_id["panel"]
        direction = cast("_PanelMoveDirection", triggered_id["direction"])

        def reorder(panels: list[PanelInstance[Any, Any]]) -> list[PanelInstance[Any, Any]]:
            return _move_panel(panels, panel_name, direction)

        page, container = _mutate_panels_and_rerender(
            page_json,
            experiment_id,
            reorder,
            view_state=EditViewState(
                full_df_json=full_df_json,
                column_kinds=column_kinds,
            ),
        )
        return container, page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input({"type": "move-chart", "panel": ALL, "index": ALL, "direction": ALL}, "n_clicks"),
        State(constants.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(FULL_DF_STORE_ID, "data", allow_optional=True),
        State(COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def move_chart(
        _n_clicks_list: list[int],
        page_json: str,
        experiment_id: int,
        full_df_json: str | None,
        column_kinds: dict[str, str] | None,
    ) -> tuple[html.Div, str]:
        triggered_id = cast("dict[str, Any]", _require_triggered_id())
        panel_name, index, direction = (
            triggered_id["panel"],
            triggered_id["index"],
            cast("_ChartMoveDirection", triggered_id["direction"]),
        )

        def reorder(panels: list[PanelInstance[Any, Any]]) -> list[PanelInstance[Any, Any]]:
            return _move_chart(panels, panel_name, index, direction)

        page, container = _mutate_panels_and_rerender(
            page_json,
            experiment_id,
            reorder,
            view_state=EditViewState(
                full_df_json=full_df_json,
                column_kinds=column_kinds,
            ),
        )
        return container, page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input({"type": "panel-sync", "panel": ALL}, "checked"),
        State(constants.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(FULL_DF_STORE_ID, "data", allow_optional=True),
        State(COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def toggle_panel_sync(
        _checked_list: list[bool],
        page_json: str,
        experiment_id: int,
        full_df_json: str | None,
        column_kinds: dict[str, str] | None,
    ) -> tuple[html.Div, str]:
        # Unlike button clicks, a Switch's `checked` is a meaningful trigger value even when
        # `False`, so this can't reuse `_require_triggered_id`'s "falsy value means no real
        # trigger" check.
        if not ctx.triggered_id:  # pyright: ignore[reportUnknownMemberType]
            raise PreventUpdate
        triggered_id = cast("dict[str, str]", ctx.triggered_id)  # pyright: ignore[reportUnknownMemberType]
        panel_name = triggered_id["panel"]
        sync = bool(cast("Any", ctx.triggered[0]["value"]))

        # Like `sync_run_selection`: this Switch's `checked` is *set from* the panel's current
        # `sync` value on every render, so a panel-sync switch mounting for the first time (e.g.
        # this page's very first render) reports that same value back as a "change" even though
        # nothing was actually toggled -- `prevent_initial_call` doesn't catch it because this
        # component isn't in the static layout. Skip the rebuild when nothing really changed.
        current_page = BasicExperimentPage.model_validate_json(page_json)
        current_sync = next((p.sync for p in current_page.panels if p.name == panel_name), None)
        if sync == current_sync:
            raise PreventUpdate

        def toggle(panels: list[PanelInstance[Any, Any]]) -> list[PanelInstance[Any, Any]]:
            return _set_panel_sync(panels, panel_name, sync=sync)

        page, container = _mutate_panels_and_rerender(
            page_json,
            experiment_id,
            toggle,
            view_state=EditViewState(
                full_df_json=full_df_json,
                column_kinds=column_kinds,
            ),
        )
        return container, page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input({"type": "panel-layout", "panel": ALL}, "value"),
        State(constants.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(FULL_DF_STORE_ID, "data", allow_optional=True),
        State(COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def toggle_panel_layout(
        _value_list: list[str],
        page_json: str,
        experiment_id: int,
        full_df_json: str | None,
        column_kinds: dict[str, str] | None,
    ) -> tuple[html.Div, str]:
        if not ctx.triggered_id:  # pyright: ignore[reportUnknownMemberType]
            raise PreventUpdate
        triggered_id = cast("dict[str, str]", ctx.triggered_id)  # pyright: ignore[reportUnknownMemberType]
        panel_name = triggered_id["panel"]
        layout = cast("typing.Literal['packed', 'grid']", ctx.triggered[0]["value"])

        # Mirrors `toggle_panel_sync`: a SegmentedControl's `value` mounting for the first time
        # reports the panel's current layout back as a "change" even though nothing was actually
        # toggled -- skip the rebuild when nothing really changed.
        current_page = BasicExperimentPage.model_validate_json(page_json)
        current_layout = next((p.layout for p in current_page.panels if p.name == panel_name), None)
        if layout == current_layout:
            raise PreventUpdate

        def toggle(panels: list[PanelInstance[Any, Any]]) -> list[PanelInstance[Any, Any]]:
            return _set_panel_layout(panels, panel_name, layout)

        page, container = _mutate_panels_and_rerender(
            page_json,
            experiment_id,
            toggle,
            view_state=EditViewState(
                full_df_json=full_df_json,
                column_kinds=column_kinds,
            ),
        )
        return container, page.model_dump_json()

    # --- rename panel ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(RENAME_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Output(RENAME_PANEL_TARGET_ID, "data"),
        Output(RENAME_PANEL_NAME_INPUT_ID, "value"),
        Output(RENAME_PANEL_ERROR_ID, "children", allow_duplicate=True),
        Input({"type": "rename-panel", "panel": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def open_rename_panel_modal(_n_clicks_list: list[int]) -> tuple[bool, str, str, str]:
        triggered_id = cast("dict[str, str]", _require_triggered_id())
        panel_name = triggered_id["panel"]
        return True, panel_name, panel_name, ""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(RENAME_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Output(RENAME_PANEL_ERROR_ID, "children", allow_duplicate=True),
        Input(RENAME_PANEL_SAVE_ID, "n_clicks"),
        State(RENAME_PANEL_TARGET_ID, "data"),
        State(RENAME_PANEL_NAME_INPUT_ID, "value"),
        State(constants.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(FULL_DF_STORE_ID, "data", allow_optional=True),
        State(COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def rename_panel(  # noqa: PLR0913
        n_clicks: int,
        old_name: str | None,
        new_name: str | None,
        page_json: str,
        experiment_id: int,
        full_df_json: str | None,
        column_kinds: dict[str, str] | None,
    ) -> tuple[Any, str | NoUpdate, bool | NoUpdate, str]:
        if not n_clicks or not old_name:
            raise PreventUpdate
        new_name = (new_name or "").strip()
        if not new_name:
            return no_update, no_update, no_update, "Panel name cannot be empty"

        curr_page = BasicExperimentPage.model_validate_json(page_json)
        if new_name != old_name and any(p.name == new_name for p in curr_page.panels):
            return no_update, no_update, no_update, f"A panel named {new_name!r} already exists"

        new_panels = [
            p.model_copy(update={"name": new_name}) if p.name == old_name else p for p in curr_page.panels
        ]
        # Carry the rename into OPEN_PANEL_KEY too, so a currently-open panel doesn't appear to
        # collapse just because its name changed underneath it.
        open_panel = curr_page.page_settings.get(OPEN_PANEL_KEY)
        new_settings = dict(curr_page.page_settings)
        if isinstance(open_panel, list):
            # Panel names (and so OPEN_PANEL_KEY) are always strings; page_settings' value type is
            # broader (shared by every settings key), hence the cast.
            open_panel = cast("list[str]", open_panel)
            new_settings[OPEN_PANEL_KEY] = [new_name if v == old_name else v for v in open_panel]
        elif open_panel == old_name:
            new_settings[OPEN_PANEL_KEY] = new_name

        store = get_data_store()
        curr_page = store.update_page(
            curr_page.model_copy(update={"panels": new_panels, "page_settings": new_settings})
        )
        container = accordion_view(
            store,
            experiment_id=experiment_id,
            view_state=EditViewState(
                full_df_json=full_df_json,
                column_kinds=column_kinds,
            ),
        )
        return container, curr_page.model_dump_json(), False, ""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(RENAME_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Input(RENAME_PANEL_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_rename_panel(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False

    # --- auto-generate charts for an empty view / suggest charts for an already-edited one ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(AUTO_POPULATE_BUTTON_ID, "n_clicks"),
        State(AUTO_POPULATE_DELIMITER_ID, "value"),
        State(AUTO_POPULATE_MODE_ID, "value"),
        State(constants.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(COLUMN_KINDS_STORE_ID, "data"),
        State(FULL_DF_STORE_ID, "data"),
        prevent_initial_call=True,
    )
    def auto_populate_charts(  # noqa: PLR0913
        n_clicks: int,
        delimiter: str | None,
        mode: str | None,
        page_json: str,
        experiment_id: int,
        column_kinds: dict[str, str] | None,
        full_df_json: str | None,
    ) -> tuple[html.Div, str]:
        if not n_clicks:
            raise PreventUpdate
        if not column_kinds:
            # Edit mode (the only thing that normally populates these caches) may never have been
            # toggled on this page load -- fetch fresh rather than wrongly reporting no data.
            full_df_json, column_kinds = _compute_full_df_and_column_kinds(get_data_store(), experiment_id)
        if not column_kinds:
            msg = "No metrics or artifacts logged for this experiment yet"
            raise ValueError(msg)
        split_mode: SplitMode = "suffix" if mode == "suffix" else "prefix"
        lightning = _is_lightning_experiment(get_data_store(), experiment_id)

        def replace_with_generated_panels(
            _panels: list[PanelInstance[Any, Any]],
        ) -> list[PanelInstance[Any, Any]]:
            return build_auto_panels(
                column_kinds, delimiter=delimiter or _DEFAULT_DELIMITER, mode=split_mode, lightning=lightning
            )

        page, container = _mutate_panels_and_rerender(
            page_json,
            experiment_id,
            replace_with_generated_panels,
            view_state=EditViewState(full_df_json=full_df_json, column_kinds=column_kinds),
        )
        return container, page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(SUGGEST_DRAWER_ID, "opened", allow_duplicate=True),
        Output(SUGGEST_CONTENT_ID, "children", allow_duplicate=True),
        Output(SUGGEST_SUGGESTIONS_STORE_ID, "data", allow_duplicate=True),
        Input(SUGGEST_CHARTS_BUTTON_ID, "n_clicks"),
        Input(SUGGEST_DELIMITER_ID, "value"),
        Input(SUGGEST_MODE_ID, "value"),
        State(constants.STATE_PAGE_STORAGE, "data"),
        State(COLUMN_KINDS_STORE_ID, "data"),
        prevent_initial_call=True,
    )
    def refresh_suggestions(
        _n_clicks: int,
        delimiter: str | None,
        mode: str | None,
        page_json: str,
        column_kinds: dict[str, str] | None,
    ) -> tuple[bool | NoUpdate, Component, list[dict[str, Any]]]:
        triggered_id = cast("str | None", ctx.triggered_id)  # pyright: ignore[reportUnknownMemberType]
        if not triggered_id:
            raise PreventUpdate
        curr_page = BasicExperimentPage.model_validate_json(page_json)
        if not column_kinds and curr_page.experiment_id is not None:
            # Edit mode (the only thing that normally populates this cache) may never have been
            # toggled on this page load -- fetch fresh rather than wrongly reporting no data.
            _full_df_json, column_kinds = _compute_full_df_and_column_kinds(
                get_data_store(), curr_page.experiment_id
            )
        if not column_kinds:
            empty = dmc.Text("No metrics or artifacts logged for this experiment yet", c="dimmed", size="sm")
            return no_update, empty, []

        split_mode: SplitMode = "suffix" if mode == "suffix" else "prefix"
        uncharted = find_uncharted_keys(curr_page.panels, column_kinds)
        lightning = curr_page.experiment_id is not None and _is_lightning_experiment(
            get_data_store(), curr_page.experiment_id
        )
        suggestions = build_suggestions(
            uncharted, delimiter=delimiter or _DEFAULT_DELIMITER, mode=split_mode, lightning=lightning
        )

        opened = True if triggered_id == SUGGEST_CHARTS_BUTTON_ID else no_update
        return (
            opened,
            _render_suggestions(suggestions),
            [
                {
                    "key": s.key,
                    "kind": s.kind.value,
                    "panel_name": s.panel_name,
                    "chart_type": s.chart.chart_type,
                    "parameters": s.chart.parameters,
                }
                for s in suggestions
            ],
        )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(SUGGEST_CONTENT_ID, "children", allow_duplicate=True),
        Output(SUGGEST_SUGGESTIONS_STORE_ID, "data", allow_duplicate=True),
        Input({"type": "add-suggestion", "kind": ALL, "key": ALL}, "n_clicks"),
        State(SUGGEST_SUGGESTIONS_STORE_ID, "data"),
        State(constants.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(FULL_DF_STORE_ID, "data", allow_optional=True),
        State(COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def add_suggested_chart(
        _n_clicks_list: list[int],
        stored_suggestions: list[dict[str, Any]] | None,
        page_json: str,
        experiment_id: int,
        full_df_json: str | None,
        column_kinds: dict[str, str] | None,
    ) -> tuple[html.Div, str, Component, list[dict[str, Any]]]:
        triggered_id = cast("dict[str, str]", _require_triggered_id())
        kind, key = triggered_id["kind"], triggered_id["key"]

        stored_suggestions = stored_suggestions or []
        match = next((s for s in stored_suggestions if s["kind"] == kind and s["key"] == key), None)
        if match is None:
            raise PreventUpdate

        chart = ChartInstance[Any, Any](chart_type=match["chart_type"], parameters=match["parameters"])
        panel_name = match["panel_name"]

        def apply_chart(panels: list[PanelInstance[Any, Any]]) -> list[PanelInstance[Any, Any]]:
            return _add_chart_to_panel_by_name(panels, panel_name, chart)

        page, container = _mutate_panels_and_rerender(
            page_json,
            experiment_id,
            apply_chart,
            view_state=EditViewState(
                full_df_json=full_df_json,
                column_kinds=column_kinds,
            ),
        )

        remaining = [s for s in stored_suggestions if not (s["kind"] == kind and s["key"] == key)]
        remaining_suggestions = [
            Suggestion(
                key=s["key"],
                kind=ColumnKind(s["kind"]),
                panel_name=s["panel_name"],
                chart=ChartInstance[Any, Any](chart_type=s["chart_type"], parameters=s["parameters"]),
            )
            for s in remaining
        ]
        return container, page.model_dump_json(), _render_suggestions(remaining_suggestions), remaining

    # --- open the modal, either to add a new chart or edit an existing one ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(ADD_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Output(ADD_CHART_TARGET_ID, "data"),
        Output(ADD_CHART_TYPE_SELECT_ID, "value"),
        Output(ADD_CHART_INITIAL_PARAMS_ID, "data"),
        Input({"type": "open-add-chart", "panel": ALL}, "n_clicks"),
        Input({"type": "edit-chart", "panel": ALL, "index": ALL}, "n_clicks"),
        State(constants.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def open_chart_modal(
        _add_clicks: list[int], _edit_clicks: list[int], page_json: str
    ) -> tuple[bool, _ChartTargetData, str | None, dict[str, Any]]:
        triggered_id = cast("_ChartID", _require_triggered_id())

        if triggered_id["type"] == "open-add-chart":
            return True, {"panel": str(triggered_id["panel"]), "index": None}, None, {}

        curr_page = BasicExperimentPage.model_validate_json(page_json)
        panel = next(p for p in curr_page.panels if p.name == triggered_id["panel"])
        idx = triggered_id["index"]
        if idx is None:
            msg = f"Malformed edit-chart id: {triggered_id}"
            raise ValueError(msg)
        chart = panel.charts[idx]
        return True, {"panel": str(triggered_id["panel"]), "index": idx}, chart.chart_type, chart.parameters

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(ADD_CHART_SUBMIT_ID, "children"),
        Output(ADD_CHART_MODAL_ID, "title"),
        Input(ADD_CHART_TARGET_ID, "data"),
        prevent_initial_call=True,
    )
    def set_modal_mode_labels(target: dict[str, Any] | None) -> tuple[str, str]:
        if not target:
            raise PreventUpdate
        if target.get("index") is None:
            return "Add chart", "Add chart"
        return "Save changes", "Edit chart"

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(ADD_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Input(ADD_CHART_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def close_add_chart_modal(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(ADD_CHART_PARAMS_ID, "children"),
        Input(ADD_CHART_TYPE_SELECT_ID, "value"),
        State(COLUMN_KINDS_STORE_ID, "data"),
        State(ADD_CHART_INITIAL_PARAMS_ID, "data"),
        prevent_initial_call=True,
    )
    def build_param_form(
        chart_type_name: str | None,
        column_kinds: dict[str, str] | None,
        initial_params: dict[str, Any] | None,
    ) -> list[Component]:
        if not chart_type_name:
            return []
        columns_by_kind = group_columns_by_kind(column_kinds or {})
        fields = ChartTypeRegistry.get_registered_chart_types()[chart_type_name]
        initial_params = initial_params or {}
        return [
            _param_field_input(name, field, columns_by_kind, override=initial_params.get(name))
            for name, field in fields.items()
        ]

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(ADD_CHART_PREVIEW_ID, "children"),
        Output(ADD_CHART_ERROR_ID, "children"),
        Input(ADD_CHART_TYPE_SELECT_ID, "value"),
        Input({"type": "chart-param", "field": ALL}, "value"),
        Input({"type": "chart-param", "field": ALL}, "checked"),
        State({"type": "chart-param", "field": ALL}, "id"),
        State(FULL_DF_STORE_ID, "data"),
        prevent_initial_call=True,
    )
    def render_preview(
        chart_type_name: str | None,
        values: list[Any],
        checked_values: list[Any],
        field_ids: list[dict[str, str]],
        df_json: str | None,
    ) -> tuple[Any, str]:
        if not chart_type_name or df_json is None:
            return None, ""

        fields = ChartTypeRegistry.get_registered_chart_types().get(chart_type_name, {})
        parameters = _merge_chart_param_values(values, checked_values, field_ids, fields)
        try:
            chart_instance = ChartInstance[Any, Any](chart_type=chart_type_name, parameters=parameters)
            df = pd.read_json(StringIO(df_json), orient="split")
            return chart_instance.render(df), ""
        except (ValidationError, KeyError, ValueError) as exc:
            _log.exception("error rendering preview")
            return None, f"Fill in required fields to see a preview ({exc})"

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
                _render_panel_content(store, experiment_id, panel_by_name[name], page.page_settings)
            )
        return outputs, sorted(loaded_set | newly_opened)
