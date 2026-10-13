"""
A plain data table: metrics and hyperparameters side by side.

Two modes:
- Default ("runs"): one row per run, showing each run's hyperparameters alongside its
  latest-logged value for each metric — the same shape as the experiment page's Runs table.
- Pivoted: set `pivot_on` (e.g. "step") to compare a single metric across runs side by side,
  one row per `pivot_on` value, one column per run.

Either way a run is named and colored as it is in the charts and the Runs table, and the grid keeps its
sort and filters (and page) when a live update redraws it: see `_table`.
"""

from __future__ import annotations

import hashlib
import typing
from pathlib import Path
from typing import Any

import dash
import dash_ag_grid as dag
import pandas as pd
from dash import MATCH, Input, Output, State
from pydantic import BaseModel, Field, model_validator

from dlboard._compat import StrEnum
from dlboard.models import (
    DEFAULT_RENDER_CONTEXT,
    RUN_NAME_COLUMN,
    ChartType,
    ColumnKind,
    MetricColumn,
    RenderContext,
)
from dlboard.plugins.charts._table_style import (
    ARTIFACT_COLUMN_PREFIX,
    HPARAM_COLUMN_PREFIX,
    TEXT,
    column_def,
    infer_column_dtype,
    themed_grid_kwargs,
)
from dlboard.serve import ClientsideScript, series_swatch_class

_IDENTITY_COLS = frozenset({MetricColumn.RUN_ID, RUN_NAME_COLUMN})
_BOOKKEEPING_COLS = frozenset({*MetricColumn, RUN_NAME_COLUMN})

_GRID_TYPE: typing.Final = "dl-table-chart"
"""The `type` of a table chart's grid id (`{"type": ..., "chart": <chart id>}`), for its page-restoring callback."""

_ROW_ID_FIELD: typing.Final = "_row_id"
_SWATCH_FIELD: typing.Final = "_swatch"
"""A row's identity and a run's swatch class, as fields of its own: dash-ag-grid's JS-string parser has no `String(...)`."""

_PAGE_JS = ClientsideScript(Path(__file__).with_name("table_chart_page.js"))


class TableMode(StrEnum):
    """What a table's rows are. Saved with a chart, so a value is never renamed (`persisted_enum_values_test`)."""

    RUNS = "runs"
    """One row per run: its hyperparameters beside the latest value of each metric."""
    PIVOT = "pivot"
    """One row per value of `pivot_on`, one column per run, comparing `pivot_metric` across runs."""


class TableChartSettings(BaseModel, frozen=True, extra="forbid"):
    """Table chart settings."""

    mode: TableMode = Field(
        default=TableMode.RUNS,
        description="What the rows are: one per run, or one per `pivot_on` value with a column per run.",
    )
    metrics: list[str] = Field(
        default_factory=list,
        description="Runs mode: metric column names to show; empty shows every metric (each run's "
        "latest value) -- unless hparams is set without this, which is read as "
        '"hparams only" rather than "every metric too".',
    )
    hparams: list[str] = Field(
        default_factory=list,
        description="Runs mode: hyperparameter keys to show; empty shows every hyperparameter.",
    )
    pivot_on: str = Field(
        default="",
        description='Pivot mode: a metric column (e.g. "step") to make the rows, comparing pivot_metric '
        "across runs side by side.",
    )
    pivot_metric: str = Field(default="", description="Pivot mode: which metric's values fill the table.")
    page_size: int = Field(default=20, ge=1, description="How many rows each page of the table shows.")
    font_size: int | None = Field(
        default=None,
        description="Cell/header font size in pixels. Leave unset to use the same (smaller) "
        "default as the rest of the charts; set this to size the table up.",
    )
    width: int | None = Field(
        default=None, description="Overall chart width in px. Leave blank to use the default flat width."
    )

    @model_validator(mode="before")
    @classmethod
    def _pivot_tables_saved_before_modes_existed(cls, data: object) -> object:
        """A table saved with `pivot_on` set was a pivot table, whatever it says about `mode`."""
        if isinstance(data, dict) and "mode" not in data and data.get("pivot_on"):
            return {**data, "mode": TableMode.PIVOT}
        return data

    @model_validator(mode="after")
    def _pivot_names_its_columns(self) -> typing.Self:
        if self.mode is TableMode.PIVOT and not (self.pivot_on and self.pivot_metric):
            msg = "pivot mode needs both `pivot_on` and `pivot_metric`"
            raise ValueError(msg)
        return self


def _table(
    column_defs: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    parameters: TableChartSettings,
    context: RenderContext,
) -> dag.AgGrid:
    """
    A grid of `rows` (each carrying its own `_ROW_ID_FIELD`) that keeps its sort and filters across redraws.

    A live update replaces the whole chart, so Dash would mount a fresh grid. It is named by the chart's
    own id and persisted per session (keyed by its columns, so a table of other columns starts clean),
    and `table_chart_page.js` puts it back on its page.
    """
    leaf_fields: list[str] = [
        leaf["field"] for column in column_defs for leaf in column.get("children", [column])
    ]
    themed_kwargs = (
        themed_grid_kwargs(font_size=f"{parameters.font_size}px")
        if parameters.font_size
        else themed_grid_kwargs()
    )
    return dag.AgGrid(
        id={"type": _GRID_TYPE, "chart": context.chart_id},
        columnDefs=column_defs,
        rowData=rows,
        # Many columns scroll sideways rather than squeezing, and every column can be filtered in place.
        defaultColDef={"minWidth": 100, "floatingFilter": True},
        columnSize="responsiveSizeToFit",
        # Keeps a row (and so its selection, were it selectable) the same row as its data changes.
        getRowId=f"params.data.{_ROW_ID_FIELD}",
        # `getRowId` and a run's swatch `cellClass` are JS expression strings -- dash-ag-grid silently
        # no-ops any string-valued JS prop unless this is set. Safe here: every such string is static and
        # written by us, never derived from user/request data.
        dangerously_allow_code=True,
        persistence=hashlib.blake2b("\0".join(leaf_fields).encode(), digest_size=5).hexdigest(),
        persisted_props=["columnState", "filterModel"],
        persistence_type="session",
        dashGridOptions={
            "pagination": True,
            "paginationPageSize": parameters.page_size,
            "paginationPageSizeSelector": sorted({parameters.page_size, 20, 50, 100}),
            "domLayout": "autoHeight",
            # A metric is free to be named `a.b`; ag-grid would otherwise read it as the field `b` of `a`.
            "suppressFieldDotNotation": True,
        },
        **themed_kwargs,
    )


def _message_table(text: str, parameters: TableChartSettings, context: RenderContext) -> dag.AgGrid:
    return _table(
        [column_def("message", TEXT)],
        [{"message": text, _ROW_ID_FIELD: "message"}],
        parameters=parameters,
        context=context,
    )


def _run_names(dataframe: pd.DataFrame) -> dict[int, str]:
    """Each run's display name, for the runs the dataframe carries one for."""
    if RUN_NAME_COLUMN not in dataframe.columns:
        return {}
    named = dataframe.dropna(subset=[RUN_NAME_COLUMN]).drop_duplicates(subset=["run_id"])
    return {
        int(run_id): str(name) for run_id, name in zip(named["run_id"], named[RUN_NAME_COLUMN], strict=True)
    }


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """`frame`'s rows as plain dicts, a gap being `None` rather than NaN."""
    return typing.cast(
        "list[dict[str, Any]]", frame.astype(object).where(frame.notna(), None).to_dict(orient="records")
    )


def _render_by_run(
    parameters: TableChartSettings, dataframe: pd.DataFrame, context: RenderContext
) -> dag.AgGrid:
    hparam_cols = [c for c in dataframe.columns if c.startswith(HPARAM_COLUMN_PREFIX)]
    if parameters.hparams:
        wanted_hparams = {f"{HPARAM_COLUMN_PREFIX}{k}" for k in parameters.hparams}
        hparam_cols = [c for c in hparam_cols if c in wanted_hparams]

    metric_cols: list[str]
    if parameters.metrics:
        metric_cols = [c for c in parameters.metrics if c in dataframe.columns and c not in _IDENTITY_COLS]
    elif parameters.hparams:
        # Curating only hparams reads as "hparams only" (see `hint_required_columns`): the panel's
        # dataframe still carries whatever metrics its sibling charts fetched, which are not ours.
        metric_cols = []
    else:
        metric_cols = [
            c
            for c in dataframe.columns
            if c not in _BOOKKEEPING_COLS
            and not c.startswith(HPARAM_COLUMN_PREFIX)
            and not c.startswith(ARTIFACT_COLUMN_PREFIX)
        ]

    # Each column's own latest value, as the Runs table shows: the wide dataframe has a row per step
    # logged by *any* metric, so a run's last row is blank for one logged less often (a per-epoch metric).
    by_step = dataframe.sort_values(["run_id", "step"]) if "step" in dataframe.columns else dataframe
    latest = by_step.groupby("run_id")[[*hparam_cols, *metric_cols]].last().reset_index()
    names = _run_names(dataframe)
    rows = [
        {
            **record,
            RUN_NAME_COLUMN: names.get(int(record["run_id"]), f"Run {record['run_id']}"),
            _ROW_ID_FIELD: str(record["run_id"]),
            _SWATCH_FIELD: series_swatch_class(int(record["run_id"])),
        }
        for record in _records(latest)
    ]

    def group(title: str, columns: list[str]) -> list[dict[str, Any]]:
        defs = [
            column_def(col, infer_column_dtype(rows, col), headerName=col.removeprefix(HPARAM_COLUMN_PREFIX))
            for col in columns
        ]
        return [{"headerName": title, "children": defs}] if defs else []

    run_column: dict[str, Any] = {
        "field": RUN_NAME_COLUMN,
        "headerName": "Run",
        "pinned": "left",
        "sortable": True,
        "filter": True,
        "minWidth": 140,
        # Each run's chart color as a dot before its name, so the table matches the charts and the Runs table.
        "cellClass": {"function": f"params.data.{_SWATCH_FIELD}"},
    }
    column_defs = [
        run_column,
        *group("Hyperparameters", hparam_cols),
        *group("Metrics (latest)", metric_cols),
    ]
    return _table(column_defs, rows, parameters=parameters, context=context)


def _render_pivoted(
    parameters: TableChartSettings, dataframe: pd.DataFrame, context: RenderContext
) -> dag.AgGrid:
    if parameters.pivot_on not in dataframe.columns or parameters.pivot_metric not in dataframe.columns:
        return _message_table("No data logged for the configured columns yet.", parameters, context)

    df = dataframe.dropna(subset=[parameters.pivot_on, parameters.pivot_metric])
    pivoted = df.pivot_table(
        index=parameters.pivot_on, columns="run_id", values=parameters.pivot_metric, aggfunc="mean"
    )
    names = _run_names(dataframe)
    run_ids = [int(run_id) for run_id in pivoted.columns]
    pivoted.columns = [str(run_id) for run_id in run_ids]
    pivoted = pivoted.reset_index()

    rows = [{**record, _ROW_ID_FIELD: str(record[parameters.pivot_on])} for record in _records(pivoted)]
    column_defs = [
        column_def(
            parameters.pivot_on, infer_column_dtype(rows, parameters.pivot_on), pinned="left", minWidth=100
        ),
        *(
            column_def(
                str(run_id),
                infer_column_dtype(rows, str(run_id)),
                headerName=names.get(run_id, f"Run {run_id}"),
                headerClass=series_swatch_class(run_id),
            )
            for run_id in run_ids
        ),
    ]
    return _table(column_defs, rows, parameters=parameters, context=context)


class TableChart(ChartType[TableChartSettings, pd.DataFrame, dag.AgGrid], frozen=True, extra="forbid"):
    """A plain data table: metrics and hyperparameters side by side."""

    name: typing.ClassVar[str] = "table"

    @classmethod
    @typing.override
    def parameter_type(cls) -> type[TableChartSettings]:
        return TableChartSettings

    @classmethod
    @typing.override
    def render(cls, parameters: TableChartSettings, dataframe: pd.DataFrame) -> dag.AgGrid:
        return cls.render_in(parameters, dataframe, DEFAULT_RENDER_CONTEXT)

    @classmethod
    @typing.override
    def render_in(
        cls, parameters: TableChartSettings, dataframe: pd.DataFrame, context: RenderContext
    ) -> dag.AgGrid:
        if dataframe.empty:
            return _message_table("No data logged yet.", parameters, context)
        match parameters.mode:
            case TableMode.RUNS:
                return _render_by_run(parameters, dataframe, context)
            case TableMode.PIVOT:
                return _render_pivoted(parameters, dataframe, context)

    @classmethod
    @typing.override
    def hint_required_columns(cls, parameters: TableChartSettings) -> set[str] | None:
        match parameters.mode:
            case TableMode.PIVOT:
                return {parameters.pivot_on, parameters.pivot_metric}
            case TableMode.RUNS:
                if parameters.metrics:
                    return set(parameters.metrics) | {"step"}
                if parameters.hparams:
                    # Metrics and hparams are fetched independently (see `hint_required_hparams`) -- having
                    # curated one but not the other reads as "I only want hparams here", not "fetch every
                    # metric in the experiment for a table that's not even configured to show them nicely".
                    return set()
                return None

    @classmethod
    @typing.override
    def hint_required_artifact_keys(cls, parameters: TableChartSettings) -> set[str]:
        return set()

    @classmethod
    @typing.override
    def hint_required_hparams(cls, parameters: TableChartSettings) -> set[str] | None:
        match parameters.mode:
            case TableMode.PIVOT:
                return set()
            case TableMode.RUNS:
                return set(parameters.hparams) if parameters.hparams else None

    @classmethod
    @typing.override
    def natural_width(cls, parameters: TableChartSettings) -> int:
        return parameters.width or 700

    @classmethod
    @typing.override
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {
            "pivot_on": ColumnKind.METRIC,
            "pivot_metric": ColumnKind.METRIC,
            "metrics": ColumnKind.METRIC,
            "hparams": ColumnKind.HPARAM,
        }


def plug(app: dash.Dash) -> None:
    """Plugin: registers the chart type, and the callback that puts a redrawn grid back on its page."""
    TableChart.register(allow_override=True)
    app.clientside_callback(
        _PAGE_JS.source,
        Output({"type": _GRID_TYPE, "chart": MATCH}, "paginationGoTo"),
        Input({"type": _GRID_TYPE, "chart": MATCH}, "paginationInfo"),
        State({"type": _GRID_TYPE, "chart": MATCH}, "id"),
    )
