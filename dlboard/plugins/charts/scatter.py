"""
A metric against a hyperparameter (or another metric), a point per run: which setting of a sweep moved the metric.

Each run is one point, at the latest value it logged of each axis (`MetricReduction.LATEST`): what it reads grows with
the runs the panel draws, never with how long they trained. Drawn with Plotly, in the runs' own colors
(`_themed_figure`), so a point is the color of the run's line in every other chart.
"""

from __future__ import annotations

import hashlib
import json
import typing

import dash_mantine_components as dmc
import pandas as pd
from pydantic import BaseModel, Field

from dlboard.models import (
    DISPLAY_SECTION,
    ChartType,
    ColumnKind,
    DataRequest,
    MetricColumn,
    MetricReduction,
    MetricReductions,
    RenderContext,
)
from dlboard.plugins.charts._chart_title import titled
from dlboard.plugins.charts._empty_chart import nothing_logged
from dlboard.plugins.charts._grouping import frame_column, latest_per_run, run_names
from dlboard.plugins.charts._themed_figure import plotly_layout, register_themed_graphs, themed_graph
from dlboard.serve import series_color

if typing.TYPE_CHECKING:
    from dash import Dash

_KIND: typing.Final = "scatter"

Scale = typing.Literal["linear", "log"]
"""How an axis spaces its values: evenly, or by order of magnitude."""


class ScatterSettings(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """Scatter settings."""

    x: str
    """A hyperparameter (`lr`) or a metric, each run's latest value of it."""
    y: str
    """A metric (or a hyperparameter), each run's latest value of it."""
    x_scale: typing.Annotated[Scale, DISPLAY_SECTION] = Field(default="linear")
    """"log" for a learning rate or a weight decay, spread over orders of magnitude."""
    y_scale: typing.Annotated[Scale, DISPLAY_SECTION] = Field(default="linear")
    """"log" for a loss spread over orders of magnitude."""
    height: typing.Annotated[int, DISPLAY_SECTION] = Field(default=360)
    width: typing.Annotated[int | None, DISPLAY_SECTION] = Field(default=None)
    """Overall chart width in px. Leave blank to size it automatically from height."""


def _instance(parameters: ScatterSettings, chart_id: str) -> str:
    raw = json.dumps([chart_id, parameters.model_dump()], sort_keys=True)
    return hashlib.shake_256(raw.encode()).hexdigest(8)


def _axis(title: str, scale: Scale) -> dict[str, typing.Any]:
    return {"title": {"text": title}, "type": scale, "automargin": True}


class ScatterChart(ChartType[ScatterSettings, pd.DataFrame, dmc.Stack], frozen=True, extra="forbid"):
    """A point per run: its latest value of `y` against its `x`, a hyperparameter or a metric."""

    name: typing.ClassVar[str] = "scatter"

    @typing.override
    @classmethod
    def parameter_type(cls) -> type[ScatterSettings]:
        return ScatterSettings

    @typing.override
    @classmethod
    def render(cls, parameters: ScatterSettings, dataframe: pd.DataFrame) -> dmc.Stack:
        return cls.render_in(parameters, dataframe, RenderContext())

    @typing.override
    @classmethod
    def render_in(
        cls, parameters: ScatterSettings, dataframe: pd.DataFrame, context: RenderContext
    ) -> dmc.Stack:
        title = f"{parameters.y} by {parameters.x}"
        x, y = frame_column(dataframe, parameters.x), frame_column(dataframe, parameters.y)
        if dataframe.empty or x not in dataframe.columns or y not in dataframe.columns:
            return dmc.Stack([nothing_logged(title)])
        per_run = latest_per_run(dataframe, [x, y])
        if per_run.empty:
            return dmc.Stack([nothing_logged(title)])
        runs = [int(run) for run in per_run[MetricColumn.RUN_ID]]
        names = run_names(dataframe)
        trace = {
            "type": "scatter",
            "mode": "markers",
            "x": per_run[x].tolist(),
            "y": per_run[y].tolist(),
            "text": [names.get(run, f"Run {run}") for run in runs],
            "marker": {"color": [series_color(run) for run in runs], "size": 11},
            "hovertemplate": f"%{{text}}<br>{parameters.x}: %{{x}}<br>{parameters.y}: %{{y}}<extra></extra>",
        }
        layout = plotly_layout(
            parameters.height,
            xaxis=_axis(parameters.x, parameters.x_scale)
            | {"gridcolor": "var(--mantine-color-default-border)"},
            yaxis=_axis(parameters.y, parameters.y_scale)
            | {"gridcolor": "var(--mantine-color-default-border)"},
            hovermode="closest",
        )
        drawn = dataframe[MetricColumn.RUN_ID].nunique()
        notes = (
            [dmc.Text(f"{len(runs)} of the {drawn} runs shown logged both", size="xs", c="dimmed")]
            if len(runs) < drawn
            else list[dmc.Text]()
        )
        graph = themed_graph(
            _KIND,
            _instance(parameters, context.chart_id),
            {"data": [trace], "layout": layout},
            parameters.height,
        )
        return titled(title, graph, *notes)

    @typing.override
    @classmethod
    def data_request(cls, parameters: ScatterSettings) -> DataRequest:
        # Either axis may be a hyperparameter or a metric, which the data says when it comes back: it is asked for as
        # both, and a key that is no hyperparameter reads nothing. A metric is read at its latest value alone.
        keys = frozenset({parameters.x, parameters.y})
        return DataRequest(
            metrics=keys, reductions=MetricReductions(default=MetricReduction.LATEST), hparams=keys
        )

    @typing.override
    @classmethod
    def natural_width(cls, parameters: ScatterSettings) -> int:
        return parameters.width or round(parameters.height * 4 / 3)

    @typing.override
    @classmethod
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"x": ColumnKind.GROUPING, "y": ColumnKind.GROUPING}


def plug(app: Dash) -> None:
    """Plugin: registers this chart type, and the browser-side step that draws its figures in the theme."""
    ScatterChart.register(allow_override=True)
    register_themed_graphs(app, _KIND)
