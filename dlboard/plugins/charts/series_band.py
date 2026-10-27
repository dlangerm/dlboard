"""
A chart of how many metrics' values spread at each step of a run, in groups of them if they are told how to group.

Drawn as a median line between the quartiles, inside the band the extremes make. Hovering a step says how many metrics
logged a value there and which ones were the lowest and the highest, so a band over thousands of metrics still points
at the ones to look at.
"""

from __future__ import annotations

import hashlib
import json
import typing
from html import escape
from pathlib import Path

import pandas as pd
from dash import MATCH, Input, Output, dcc, html
from dash.development.base_component import Component
from pydantic import BaseModel, Field

from dlboard.models import (
    ADVANCED_SECTION,
    DEFAULT_RENDER_CONTEXT,
    DISPLAY_SECTION,
    RUN_NAME_COLUMN,
    ChartType,
    ColumnKind,
    DataRequest,
    MetricColumn,
    NamePatternList,
    RenderContext,
    SeriesBand,
    SeriesBandRequest,
)
from dlboard.plugins.charts._band_traces import nested_band
from dlboard.serve import ClientsideScript, series_color

if typing.TYPE_CHECKING:
    from dash import Dash

_COLORS_JS: typing.Final = ClientsideScript(Path(__file__).with_name("series_band_colors.js"))
_FIGURE_TYPE: typing.Final = "series-band-figure"
_GRAPH_TYPE: typing.Final = "series-band-graph"
_RUN_DASHES: typing.Final = ("solid", "dash", "dot")
"""How a run is told from the others when the groups, not the runs, are what the colors are for."""

_FILL_ALPHA: typing.Final = 0.12
"""How opaque the band of extremes is, against the median line drawn over it (the quartiles' band is twice that)."""


def _opaque_to(color: str, opacity: float) -> str:
    """A theme color (`series_color`) at `opacity`, in the form `series_band_colors.js` resolves in the browser."""
    return f"{color}/{opacity}"


class SeriesBandSettings(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """Series band settings."""

    metrics: NamePatternList = Field(default_factory=list)
    """The metrics whose values are spread: names, and patterns (`layer_*/grad_norm`, `accuracy/#,#/*`) for many at once."""
    group_by: typing.Annotated[int | None, DISPLAY_SECTION] = Field(default=None, ge=0)
    """Which `*` or `#` of the pattern (0 for the first) names the group a metric is in, so `accuracy/#,#/*` grouped by 2 is a band for each class. Leave blank for one band over every metric."""
    grid: typing.Annotated[int, DISPLAY_SECTION] = Field(default=32, ge=2, le=64)
    """At most this many steps are drawn, fewer over thousands of metrics, which cost a read each. The grid is as fine as that allows, and always ends on a run's last step."""
    runs: typing.Annotated[int, DISPLAY_SECTION] = Field(default=1, ge=1, le=3)
    """How many of the panel's newest runs are drawn. Each one costs a read of every metric the band spreads over."""
    show_mean: typing.Annotated[bool, DISPLAY_SECTION] = False
    """Draw the mean as a thinner dotted line beside the median. Across metrics of different scales it follows the largest."""
    max_groups: typing.Annotated[int, ADVANCED_SECTION] = Field(default=12, ge=1, le=24)
    """The most groups drawn, by their names in order: each is a band for each run, and more are unreadable."""
    height: typing.Annotated[int, DISPLAY_SECTION] = 300
    width: typing.Annotated[int | None, DISPLAY_SECTION] = Field(default=None)
    """Overall chart width in px. Leave blank to size it automatically from height."""


def _request(parameters: SeriesBandSettings) -> SeriesBandRequest:
    return SeriesBandRequest(
        patterns=frozenset(parameters.metrics),
        grid=parameters.grid,
        runs=parameters.runs,
        group_by=() if parameters.group_by is None else (parameters.group_by,),
        max_groups=parameters.max_groups,
    )


def _bands(dataframe: pd.DataFrame, column: str) -> list[SeriesBand]:
    """The bands the panel read for this chart, in run, group and step order (a row without any is skipped)."""
    if column not in dataframe.columns:
        return []
    return sorted(
        (band for cell in dataframe[column].dropna() for band in cell),
        key=lambda band: (band.run_id, band.group, band.step),
    )


def _hover(label: str, band: SeriesBand, *, mean: bool) -> str:
    """What a step says: how many metrics logged a value, and the spread of them with the extremes named."""
    return "<br>".join(
        [
            f"<b>{escape(label)}</b>: {band.count} metrics",
            f"max {band.maximum:.4g} ({escape(band.maximum_key)})",
            f"75% {band.p75:.4g}",
            f"median {band.median:.4g}",
            f"25% {band.p25:.4g}",
            f"min {band.minimum:.4g} ({escape(band.minimum_key)})",
            *([f"mean {band.mean:.4g}"] if mean else []),
        ]
    )


def _traces(
    bands: list[SeriesBand], names: dict[int, str], *, show_mean: bool
) -> list[dict[str, typing.Any]]:
    """
    Per run and group: the extremes, the quartiles and the median, each a trace over its grid steps.

    The colors are the theme's own (`series_color`, which Plotly cannot read: the browser resolves them), so a run is
    the color it is in every other chart. With several groups it is the groups that get the colors, and the runs are
    told apart by how their lines are dashed.
    """
    series: dict[tuple[int, str], list[SeriesBand]] = {}
    for band in bands:
        series.setdefault((band.run_id, band.group), []).append(band)
    runs = list(dict.fromkeys(run_id for run_id, _ in series))
    groups = sorted({group for _, group in series})
    traces: list[dict[str, typing.Any]] = []
    for (run_id, group), rows in series.items():
        color = series_color(groups.index(group) if len(groups) > 1 else run_id)
        dash = _RUN_DASHES[runs.index(run_id) % len(_RUN_DASHES)] if len(groups) > 1 else "solid"
        run_label = names.get(run_id, f"Run {run_id}")
        label = " · ".join(part for part in (run_label if len(runs) > 1 or not group else "", group) if part)
        steps = [band.step for band in rows]
        traces += nested_band(
            steps,
            highs=[band.maximum for band in rows],
            near_highs=[band.p75 for band in rows],
            medians=[band.median for band in rows],
            near_lows=[band.p25 for band in rows],
            lows=[band.minimum for band in rows],
            outer_fill=_opaque_to(color, _FILL_ALPHA),
            inner_fill=_opaque_to(color, _FILL_ALPHA * 2),
            median_line={"width": 2, "color": color, "dash": dash},
            shared={"legendgroup": label, "showlegend": False},
            median_extra={
                "name": label,
                "showlegend": True,
                "text": [_hover(label, band, mean=show_mean) for band in rows],
                "hovertemplate": "%{text}<extra></extra>",
            },
        )
        if show_mean:
            traces.append(
                {
                    "type": "scatter",
                    "mode": "lines",
                    "x": steps,
                    "y": [band.mean for band in rows],
                    "line": {"width": 1.5, "dash": "dot", "color": color},
                    "legendgroup": label,
                    "showlegend": False,
                    "hoverinfo": "skip",
                }
            )
    return traces


def _instance(parameters: SeriesBandSettings, chart_id: str) -> str:
    """
    One id per chart and per settings, for the ids the browser-side colors are found by.

    The chart's own id is part of it, as for an image chart: the editor's preview renders a chart with the very
    settings it is about to be saved with, and a callback cannot resolve two components under one id.
    """
    return hashlib.shake_256(
        json.dumps([chart_id, parameters.model_dump()], sort_keys=True).encode()
    ).hexdigest(8)


class SeriesBandChart(ChartType[SeriesBandSettings, pd.DataFrame, Component], frozen=True, extra="forbid"):
    """The spread of many metrics' values at each step: a median line between quartiles, inside the extremes."""

    name: typing.ClassVar[str] = "series_band"

    @typing.override
    @classmethod
    def parameter_type(cls) -> type[SeriesBandSettings]:
        return SeriesBandSettings

    @classmethod
    @typing.override
    def render(cls, parameters: SeriesBandSettings, dataframe: pd.DataFrame) -> Component:
        return cls.render_in(parameters, dataframe, DEFAULT_RENDER_CONTEXT)

    @classmethod
    @typing.override
    def render_in(
        cls, parameters: SeriesBandSettings, dataframe: pd.DataFrame, context: RenderContext
    ) -> Component:
        bands = _bands(dataframe, _request(parameters).column)
        if not bands:
            return dcc.Graph(
                figure={"data": [], "layout": {"height": parameters.height}}, style={"display": "none"}
            )
        names: dict[int, str] = {}
        if RUN_NAME_COLUMN in dataframe.columns and MetricColumn.RUN_ID in dataframe.columns:
            named = dataframe.dropna(subset=[RUN_NAME_COLUMN]).drop_duplicates(subset=[MetricColumn.RUN_ID])
            names = dict(zip(named[MetricColumn.RUN_ID], named[RUN_NAME_COLUMN], strict=True))
        text, grid = "var(--mantine-color-text)", "var(--mantine-color-default-border)"
        figure = {
            "data": _traces(bands, names, show_mean=parameters.show_mean),
            "layout": {
                "height": parameters.height,
                "margin": {"l": 50, "r": 20, "t": 20, "b": 40},
                "font": {"color": text},
                "xaxis": {"title": {"text": "step"}, "gridcolor": grid, "linecolor": grid},
                "yaxis": {"zeroline": False, "gridcolor": grid, "linecolor": grid},
                "hovermode": "x",
                "legend": {"orientation": "h", "y": -0.2},
                "paper_bgcolor": "rgba(0,0,0,0)",
                "plot_bgcolor": "rgba(0,0,0,0)",
            },
        }
        instance = _instance(parameters, context.chart_id)
        # The graph starts empty and the figure waits in a store: Plotly drops a color it cannot parse (a CSS
        # variable), so `series_band_colors.js` hands it the figure with the theme's colors in place of them.
        return html.Div(
            [
                dcc.Store(id={"type": _FIGURE_TYPE, "instance": instance}, data=figure),
                dcc.Graph(
                    id={"type": _GRAPH_TYPE, "instance": instance},
                    figure={"data": [], "layout": {"height": parameters.height}},
                    config={"displayModeBar": False, "responsive": True},
                    style={"height": f"{parameters.height}px", "width": "100%"},
                ),
            ]
        )

    @classmethod
    @typing.override
    def data_request(cls, parameters: SeriesBandSettings) -> DataRequest:
        # The spread is read in place of the metrics themselves, so the panel reads none of them for this chart.
        return DataRequest(series=frozenset({_request(parameters)} if parameters.metrics else ()))

    @classmethod
    @typing.override
    def natural_width(cls, parameters: SeriesBandSettings) -> int:
        return parameters.width or round(parameters.height * 16 / 9)

    @classmethod
    @typing.override
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"metrics": ColumnKind.METRIC}


def plug(app: Dash) -> None:
    """Plugin: registers this chart type, and the browser-side step that gives its figures the theme's colors."""
    SeriesBandChart.register(allow_override=True)
    app.clientside_callback(
        _COLORS_JS.source,
        Output({"type": _GRAPH_TYPE, "instance": MATCH}, "figure"),
        Input({"type": _FIGURE_TYPE, "instance": MATCH}, "data"),
    )
