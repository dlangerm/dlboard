"""
A sweep's hyperparameters and where each run ended up, side by side: an axis per hyperparameter and one for the metric.

A line per run crosses each axis at its value, coloured by its latest value of the metric, so the settings of the runs
that did best line up. Brushing an axis (dragging along it) keeps the lines through that range, in Plotly itself, on
what the browser already holds. What it reads is a row per run (`MetricReduction.LATEST`, and each run's
hyperparameters): it grows with the runs the panel draws, never with how long they trained.
"""

from __future__ import annotations

import hashlib
import json
import typing

import dash_mantine_components as dmc
import pandas as pd
from pydantic import BaseModel, Field

from dlboard.models import (
    ADVANCED_SECTION,
    DISPLAY_SECTION,
    EVERY_KEY,
    HPARAM_COLUMN_PREFIX,
    ChartType,
    ColumnKind,
    DataRequest,
    MetricColumn,
    MetricReduction,
    MetricReductions,
    RenderContext,
    hparam_column,
)
from dlboard.plugins.charts._chart_title import titled
from dlboard.plugins.charts._empty_chart import nothing_logged
from dlboard.plugins.charts._grouping import frame_column
from dlboard.plugins.charts._themed_figure import plotly_layout, register_themed_graphs, themed_graph

if typing.TYPE_CHECKING:
    from dash import Dash

_KIND: typing.Final = "parallel-coordinates"
_MISSING: typing.Final = "(none)"
"""A run's value of a hyperparameter it was not given, on a categorical axis."""


class ParallelCoordinatesSettings(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """Parallel coordinates settings."""

    metric: str
    """The metric each run's line is coloured by, and the last axis: its latest value."""
    hparams: list[str] = Field(default_factory=list[str])
    """The hyperparameters to give an axis each, in order. Leave empty for every one the runs shown do not all share."""
    max_axes: typing.Annotated[int, ADVANCED_SECTION] = Field(default=12, ge=1, le=32)
    """The most hyperparameter axes drawn: past a dozen the lines are too close to follow."""
    height: typing.Annotated[int, DISPLAY_SECTION] = Field(default=420)
    width: typing.Annotated[int, DISPLAY_SECTION] = Field(default=960)
    """Overall chart width in px."""


def _instance(parameters: ParallelCoordinatesSettings, chart_id: str) -> str:
    raw = json.dumps([chart_id, parameters.model_dump()], sort_keys=True)
    return hashlib.shake_256(raw.encode()).hexdigest(8)


def per_run(dataframe: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """A row per run: the latest value it logged of each of `columns` (empty where it has none)."""
    ordered = (
        dataframe.sort_values(MetricColumn.STEP) if MetricColumn.STEP in dataframe.columns else dataframe
    )
    return ordered.groupby(MetricColumn.RUN_ID)[columns].last().reset_index()


def _dimension(label: str, values: pd.Series) -> dict[str, typing.Any]:
    """An axis: numbers where every value is one, else the values as categories, in order."""
    numeric = pd.to_numeric(values, errors="coerce")
    if numeric.notna().all():
        return {"label": label, "values": numeric.tolist()}
    shown = [_MISSING if pd.isna(value) else str(value) for value in values.tolist()]
    categories = sorted(set(shown))
    return {
        "label": label,
        "values": [categories.index(value) for value in shown],
        "tickvals": list(range(len(categories))),
        "ticktext": categories,
    }


def chosen_axes(parameters: ParallelCoordinatesSettings, runs: pd.DataFrame) -> tuple[list[str], int]:
    """The hyperparameters given an axis, by key, and how many more differ than `max_axes` lets on."""
    if parameters.hparams:
        named = [key for key in parameters.hparams if hparam_column(key) in runs.columns]
        return named[: parameters.max_axes], max(0, len(named) - parameters.max_axes)
    differing = sorted(
        column.removeprefix(HPARAM_COLUMN_PREFIX)
        for column in runs.columns
        if column.startswith(HPARAM_COLUMN_PREFIX) and runs[column].astype(str).nunique() > 1
    )
    return differing[: parameters.max_axes], max(0, len(differing) - parameters.max_axes)


class ParallelCoordinatesChart(
    ChartType[ParallelCoordinatesSettings, pd.DataFrame, dmc.Stack], frozen=True, extra="forbid"
):
    """An axis per hyperparameter and one for the metric, a line per run through its values, coloured by the metric."""

    name: typing.ClassVar[str] = "parallel_coordinates"

    @typing.override
    @classmethod
    def parameter_type(cls) -> type[ParallelCoordinatesSettings]:
        return ParallelCoordinatesSettings

    @typing.override
    @classmethod
    def render(cls, parameters: ParallelCoordinatesSettings, dataframe: pd.DataFrame) -> dmc.Stack:
        return cls.render_in(parameters, dataframe, RenderContext())

    @typing.override
    @classmethod
    def render_in(
        cls, parameters: ParallelCoordinatesSettings, dataframe: pd.DataFrame, context: RenderContext
    ) -> dmc.Stack:
        metric = frame_column(dataframe, parameters.metric)
        if dataframe.empty or metric not in dataframe.columns:
            return dmc.Stack([nothing_logged(parameters.metric)])
        columns = [metric, *(c for c in dataframe.columns if c.startswith(HPARAM_COLUMN_PREFIX))]
        runs = per_run(dataframe, columns).dropna(subset=[metric])
        axes, more = chosen_axes(parameters, runs)
        if runs.empty or not axes:
            text = "The runs shown share every hyperparameter" if not runs.empty else None
            return dmc.Stack([dmc.Text(text, c="dimmed")] if text else [nothing_logged(parameters.metric)])
        text_color = "var(--mantine-color-text)"
        trace = {
            "type": "parcoords",
            "dimensions": [
                *(_dimension(key, runs[hparam_column(key)]) for key in axes),
                {"label": parameters.metric, "values": runs[metric].tolist()},
            ],
            "line": {
                "color": runs[metric].tolist(),
                "colorscale": "Viridis",
                "showscale": True,
                "colorbar": {"title": {"text": parameters.metric}, "tickfont": {"color": text_color}},
            },
            "labelfont": {"color": text_color},
            "tickfont": {"color": text_color},
            "rangefont": {"color": text_color},
        }
        layout = plotly_layout(parameters.height, margin={"l": 60, "r": 60, "t": 60, "b": 30})
        notes = [
            dmc.Text(f"{len(runs)} runs drawn: the panel's", size="xs", c="dimmed"),
            *(
                [
                    dmc.Text(
                        f"{more} more hyperparameters differ: name the ones to draw in `hparams`",
                        size="xs",
                        c="dimmed",
                    )
                ]
                if more
                else []
            ),
        ]
        graph = themed_graph(
            _KIND,
            _instance(parameters, context.chart_id),
            {"data": [trace], "layout": layout},
            parameters.height,
        )
        return titled(f"{parameters.metric} by hyperparameter", graph, *notes)

    @typing.override
    @classmethod
    def data_request(cls, parameters: ParallelCoordinatesSettings) -> DataRequest:
        # A row per run: the metric's latest value, and the hyperparameters named (every one, to find those that differ).
        return DataRequest(
            metrics=frozenset({parameters.metric}),
            reductions=MetricReductions(default=MetricReduction.LATEST),
            hparams=frozenset(parameters.hparams),
            hparam_patterns=frozenset[str]() if parameters.hparams else EVERY_KEY,
        )

    @typing.override
    @classmethod
    def natural_width(cls, parameters: ParallelCoordinatesSettings) -> int:
        return parameters.width

    @typing.override
    @classmethod
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"metric": ColumnKind.METRIC, "hparams": ColumnKind.HPARAM}


def plug(app: Dash) -> None:
    """Plugin: registers this chart type, and the browser-side step that draws its figures in the theme."""
    ParallelCoordinatesChart.register(allow_override=True)
    register_themed_graphs(app, _KIND)
