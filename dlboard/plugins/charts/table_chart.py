"""
A plain data table chart: metrics and hyperparameters side by side.

Two modes:
- Default ("runs"): one row per run, showing each run's hyperparameters alongside its
  last-logged value for each metric — the same shape as the experiment page's Runs table.
- Pivoted: set `pivot_on` (e.g. "step") to compare a single metric across runs side by side,
  one row per `pivot_on` value, one column per run.
"""

from __future__ import annotations

import typing
from typing import Any

import dash_ag_grid as dag
import pandas as pd
from pydantic import BaseModel, Field

from dlboard.models import RUN_NAME_COLUMN, ChartType, ColumnKind, MetricColumn
from dlboard.plugins.charts._table_style import (
    ARTIFACT_COLUMN_PREFIX,
    HPARAM_COLUMN_PREFIX,
    column_def,
    infer_column_dtype,
    themed_grid_kwargs,
)

if typing.TYPE_CHECKING:
    from dash import Dash

_IDENTITY_COLS = frozenset({MetricColumn.RUN_ID, RUN_NAME_COLUMN})
_BOOKKEEPING_COLS = frozenset({*MetricColumn, RUN_NAME_COLUMN})


class TableChartSettings(BaseModel, frozen=True, extra="forbid"):
    """Table chart settings."""

    metrics: list[str] = Field(
        default_factory=list,
        description="Metric column names to show; empty shows every metric (run mode uses each "
        "run's last-logged value) -- unless hparams is set without this, which is read as "
        '"hparams only" rather than "every metric too".',
    )
    hparams: list[str] = Field(
        default_factory=list, description="Hyperparameter keys to show; empty shows every hyperparameter."
    )
    pivot_on: str = Field(
        default="",
        description='A metric column (e.g. "step") to pivot rows around, comparing pivot_metric '
        "across runs side by side. Leave empty for the default: one row per run.",
    )
    pivot_metric: str = Field(
        default="",
        description="Which metric's values fill the table when pivot_on is set. Ignored otherwise.",
    )
    page_size: int = 20
    font_size: int | None = Field(
        default=None,
        description="Cell/header font size in pixels. Leave unset to use the same (smaller) "
        "default as the rest of the charts; set this to size the table up.",
    )
    width: int | None = Field(
        default=None, description="Overall chart width in px. Leave blank to use the default flat width."
    )


def _message_table(text: str, *, page_size: int, font_size: int | None) -> dag.AgGrid:
    return _build_table([{"message": text}], ["message"], page_size=page_size, font_size=font_size)


def _build_table(
    rows: list[dict[str, Any]],
    columns: list[str],
    *,
    page_size: int,
    font_size: int | None,
    headers: dict[str, str] | None = None,
) -> dag.AgGrid:
    """A grid of `rows`, one column per field in `columns`, titled by `headers` where it names the field."""
    headers = headers or {}
    column_defs = [
        column_def(col, infer_column_dtype(rows, col), headerName=headers.get(col, col)) for col in columns
    ]
    themed_kwargs = themed_grid_kwargs(font_size=f"{font_size}px") if font_size else themed_grid_kwargs()
    return dag.AgGrid(
        columnDefs=column_defs,
        rowData=rows,
        columnSize="responsiveSizeToFit",
        dashGridOptions={
            "pagination": True,
            "paginationPageSize": page_size,
            "paginationPageSizeSelector": sorted({page_size, 20, 50, 100}),
            "domLayout": "autoHeight",
            # A metric is free to be named `a.b`; ag-grid would otherwise read it as the field `b` of `a`.
            "suppressFieldDotNotation": True,
        },
        **themed_kwargs,
    )


def _render_by_run(parameters: TableChartSettings, dataframe: pd.DataFrame) -> dag.AgGrid:
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
    rows = typing.cast(
        "list[dict[str, Any]]", latest.astype(object).where(latest.notna(), None).to_dict(orient="records")
    )

    headers = {c: c.removeprefix(HPARAM_COLUMN_PREFIX) for c in hparam_cols}
    return _build_table(
        rows,
        ["run_id", *hparam_cols, *metric_cols],
        page_size=parameters.page_size,
        font_size=parameters.font_size,
        headers=headers,
    )


def _render_pivoted(parameters: TableChartSettings, dataframe: pd.DataFrame) -> dag.AgGrid:
    if not parameters.pivot_metric:
        return _message_table(
            "Set pivot_metric to compare a metric across runs.",
            page_size=parameters.page_size,
            font_size=parameters.font_size,
        )
    if parameters.pivot_on not in dataframe.columns or parameters.pivot_metric not in dataframe.columns:
        return _message_table(
            "No data logged for the configured columns yet.",
            page_size=parameters.page_size,
            font_size=parameters.font_size,
        )

    df = dataframe.dropna(subset=[parameters.pivot_on, parameters.pivot_metric])
    pivoted = df.pivot_table(
        index=parameters.pivot_on, columns="run_id", values=parameters.pivot_metric, aggfunc="mean"
    ).reset_index()
    pivoted.columns = [str(c) for c in pivoted.columns]  # pyrefly: ignore [unnecessary-type-conversion]

    rows = typing.cast("list[dict[str, Any]]", pivoted.to_dict(orient="records"))
    return _build_table(
        rows, list(pivoted.columns), page_size=parameters.page_size, font_size=parameters.font_size
    )


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
        if dataframe.empty:
            return _message_table(
                "No data logged yet.", page_size=parameters.page_size, font_size=parameters.font_size
            )
        if parameters.pivot_on:
            return _render_pivoted(parameters, dataframe)
        return _render_by_run(parameters, dataframe)

    @classmethod
    @typing.override
    def hint_required_columns(cls, parameters: TableChartSettings) -> set[str] | None:
        if parameters.pivot_on:
            return {c for c in (parameters.pivot_on, parameters.pivot_metric) if c}
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
        if parameters.pivot_on:
            return set()
        if not parameters.hparams:
            return None
        return set(parameters.hparams)

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


def plug(app: Dash) -> None:  # noqa: ARG001
    """Plugin."""
    TableChart.register(allow_override=True)
