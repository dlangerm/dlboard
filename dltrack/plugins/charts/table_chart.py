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

import pandas as pd
from dash import dash_table
from dash.dash_table.Format import Format
from pydantic import BaseModel, Field

from dltrack.models import ChartType, ColumnKind
from dltrack.plugins.charts._grouping import last_row_per_run
from dltrack.plugins.charts._table_style import (
    HPARAM_COLUMN_PREFIX,
    NUMERIC,
    TAGS_COLUMN_SUFFIX,
    infer_column_dtype,
    themed_datatable_kwargs,
)

if typing.TYPE_CHECKING:
    from dash import Dash

_BOOKKEEPING_COLS = frozenset({"run_id", "index", "timestamp_utc", "experiment_id"})


class TableChartSettings(BaseModel, frozen=True, extra="forbid"):
    """Table chart settings."""

    metrics: list[str] = Field(
        default=[],
        description="Metric column names to show; empty shows every metric (run mode uses each "
        "run's last-logged value).",
    )
    hparams: list[str] = Field(
        default=[], description="Hyperparameter keys to show; empty shows every hyperparameter."
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


def _message_table(text: str, *, page_size: int, font_size: int | None) -> dash_table.DataTable:
    return _build_table([{"message": text}], ["message"], page_size=page_size, font_size=font_size)


def _build_table(
    rows: list[dict[str, Any]], columns: list[str], *, page_size: int, font_size: int | None
) -> dash_table.DataTable:
    column_defs: list[dict[str, Any]] = []
    for col in columns:
        col_def: dict[str, Any] = {"name": col, "id": col, "type": "text"}
        if infer_column_dtype(rows, col) == NUMERIC:
            col_def["type"] = "numeric"
            col_def["format"] = Format(precision=3, scheme="s")
        column_defs.append(col_def)

    themed_kwargs = (
        themed_datatable_kwargs(font_size=f"{font_size}px") if font_size else themed_datatable_kwargs()
    )
    return dash_table.DataTable(
        columns=column_defs,  # pyright: ignore[reportArgumentType]
        data=rows,  # pyright: ignore[reportArgumentType]
        filter_action="native",
        sort_action="native",
        page_action="native",
        page_size=page_size,
        **themed_kwargs,
    )


def _render_by_run(parameters: TableChartSettings, dataframe: pd.DataFrame) -> dash_table.DataTable:
    hparam_cols = [c for c in dataframe.columns if c.startswith(HPARAM_COLUMN_PREFIX)]
    if parameters.hparams:
        wanted_hparams = {f"{HPARAM_COLUMN_PREFIX}{k}" for k in parameters.hparams}
        hparam_cols = [c for c in hparam_cols if c in wanted_hparams]

    metric_cols = [
        c
        for c in dataframe.columns
        if c not in _BOOKKEEPING_COLS
        and not c.startswith(HPARAM_COLUMN_PREFIX)
        and not c.endswith(TAGS_COLUMN_SUFFIX)
    ]
    if parameters.metrics:
        wanted_metrics = set(parameters.metrics)
        metric_cols = [c for c in metric_cols if c in wanted_metrics]

    last_rows = last_row_per_run(dataframe)

    rows: list[dict[str, Any]] = []
    for _, row in last_rows.iterrows():
        record: dict[str, Any] = {"run_id": int(row["run_id"])}
        for col in hparam_cols:
            value = row[col]
            record[col.removeprefix(HPARAM_COLUMN_PREFIX)] = None if pd.isna(value) else value
        for col in metric_cols:
            value = row[col]
            record[col] = None if pd.isna(value) else value
        rows.append(record)

    columns = ["run_id", *(c.removeprefix(HPARAM_COLUMN_PREFIX) for c in hparam_cols), *metric_cols]
    return _build_table(rows, columns, page_size=parameters.page_size, font_size=parameters.font_size)


def _render_pivoted(parameters: TableChartSettings, dataframe: pd.DataFrame) -> dash_table.DataTable:
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
    pivoted.columns = [str(c) for c in pivoted.columns]

    rows = typing.cast("list[dict[str, Any]]", pivoted.to_dict(orient="records"))
    return _build_table(
        rows, list(pivoted.columns), page_size=parameters.page_size, font_size=parameters.font_size
    )


class TableChart(
    ChartType[TableChartSettings, pd.DataFrame, dash_table.DataTable], frozen=True, extra="forbid"
):
    """A plain data table: metrics and hyperparameters side by side."""

    name: typing.ClassVar[str] = "table"

    @classmethod
    @typing.override
    def parameter_type(cls) -> type[TableChartSettings]:
        return TableChartSettings

    @classmethod
    @typing.override
    def render(cls, parameters: TableChartSettings, dataframe: pd.DataFrame) -> dash_table.DataTable:
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
        if not parameters.metrics:
            return None
        return set(parameters.metrics) | {"step"}

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
