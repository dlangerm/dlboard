"""
A chart of how many metrics' values spread at each step of a run, in groups of them if they are told how to group.

Drawn as a median line between the quartiles, inside the band the extremes make. Hovering a step says how many metrics
logged a value there and which ones were the lowest and the highest, so a band over thousands of metrics still points
at the ones to look at.
"""

from __future__ import annotations

import html
import typing

import pandas as pd
import plotly.colors
from dash import dcc
from dash.development.base_component import Component
from pydantic import BaseModel, Field

from dlboard.models import (
    ADVANCED_SECTION,
    DISPLAY_SECTION,
    RUN_NAME_COLUMN,
    ChartType,
    ColumnKind,
    MetricColumn,
    NamePatternList,
    SeriesBand,
    SeriesBandRequest,
)
from dlboard.plugins.charts._band_traces import nested_band

if typing.TYPE_CHECKING:
    from dash import Dash

_FILL_ALPHA: typing.Final = 0.12
"""How opaque the band of extremes is, against the median line drawn over it (the quartiles' band is twice that)."""


class SeriesBandSettings(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """Series band settings."""

    metrics: NamePatternList = Field(default_factory=list)
    """The metrics whose values are spread: names, and patterns (`layer_*/grad_norm`, `accuracy/#,#/*`) for many at once."""
    group_by: typing.Annotated[int | None, DISPLAY_SECTION] = Field(default=None, ge=0)
    """Which `*` or `#` of the pattern (0 for the first) names the group a metric is in, so `accuracy/#,#/*` grouped by 2 is a band for each class. Leave blank for one band over every metric."""
    grid: typing.Annotated[int, DISPLAY_SECTION] = Field(default=32, ge=2, le=64)
    """At most this many steps are drawn. The grid is as fine as that allows, and always ends on a run's last step."""
    runs: typing.Annotated[int, DISPLAY_SECTION] = Field(default=1, ge=1, le=3)
    """How many of the panel's newest runs are drawn. Each one costs a read of every metric the band spreads over."""
    show_mean: typing.Annotated[bool, DISPLAY_SECTION] = False
    """Draw the mean as a dashed line beside the median. Across metrics of different scales it follows the largest."""
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
            f"<b>{html.escape(label)}</b>: {band.count} metrics",
            f"max {band.maximum:.4g} ({html.escape(band.maximum_key)})",
            f"75% {band.p75:.4g}",
            f"median {band.median:.4g}",
            f"25% {band.p25:.4g}",
            f"min {band.minimum:.4g} ({html.escape(band.minimum_key)})",
            *([f"mean {band.mean:.4g}"] if mean else []),
        ]
    )


def _traces(
    bands: list[SeriesBand], names: dict[int, str], *, show_mean: bool
) -> list[dict[str, typing.Any]]:
    """Per run and group: the extremes, the quartiles and the median, each a trace over its grid steps."""
    series: dict[tuple[int, str], list[SeriesBand]] = {}
    for band in bands:
        series.setdefault((band.run_id, band.group), []).append(band)
    runs = list(dict.fromkeys(run_id for run_id, _ in series))
    groups = sorted({group for _, group in series})
    # Plotly cannot read the theme's series colors (CSS variables), so it draws from its own palette: a color for each
    # group if there are several, else for each run.
    palette = plotly.colors.qualitative.Plotly
    traces: list[dict[str, typing.Any]] = []
    for (run_id, group), rows in series.items():
        index = groups.index(group) if len(groups) > 1 else runs.index(run_id)
        red, green, blue = plotly.colors.hex_to_rgb(palette[index % len(palette)])
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
            outer_fill=f"rgba({red},{green},{blue},{_FILL_ALPHA})",
            inner_fill=f"rgba({red},{green},{blue},{_FILL_ALPHA * 2})",
            median_line={"width": 2, "color": f"rgb({red},{green},{blue})"},
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
                    "line": {"width": 1.5, "dash": "dash", "color": f"rgb({red},{green},{blue})"},
                    "legendgroup": label,
                    "showlegend": False,
                    "hoverinfo": "skip",
                }
            )
    return traces


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
        bands = _bands(dataframe, _request(parameters).column)
        if not bands:
            return dcc.Graph(
                figure={"data": [], "layout": {"height": parameters.height}}, style={"display": "none"}
            )
        names: dict[int, str] = {}
        if RUN_NAME_COLUMN in dataframe.columns and MetricColumn.RUN_ID in dataframe.columns:
            named = dataframe.dropna(subset=[RUN_NAME_COLUMN]).drop_duplicates(subset=[MetricColumn.RUN_ID])
            names = dict(zip(named[MetricColumn.RUN_ID], named[RUN_NAME_COLUMN], strict=True))
        figure = {
            "data": _traces(bands, names, show_mean=parameters.show_mean),
            "layout": {
                "height": parameters.height,
                "margin": {"l": 50, "r": 20, "t": 20, "b": 40},
                "xaxis": {"title": {"text": "step"}},
                "yaxis": {"zeroline": False},
                "hovermode": "x",
                "legend": {"orientation": "h", "y": -0.2},
                "paper_bgcolor": "rgba(0,0,0,0)",
                "plot_bgcolor": "rgba(0,0,0,0)",
            },
        }
        return dcc.Graph(
            figure=figure,
            config={"displayModeBar": False, "responsive": True},
            style={"height": f"{parameters.height}px", "width": "100%"},
        )

    @classmethod
    @typing.override
    def hint_required_columns(cls, parameters: SeriesBandSettings) -> set[str] | None:
        # The band reads its own metrics (see `hint_required_series`), so the panel fetches none for this chart.
        return set()

    @classmethod
    @typing.override
    def hint_required_series(cls, parameters: SeriesBandSettings) -> frozenset[SeriesBandRequest]:
        return frozenset({_request(parameters)}) if parameters.metrics else frozenset()

    @classmethod
    @typing.override
    def hint_required_artifact_keys(cls, parameters: SeriesBandSettings) -> set[str]:
        return set()

    @classmethod
    @typing.override
    def hint_required_hparams(cls, parameters: SeriesBandSettings) -> set[str]:
        return set()

    @classmethod
    @typing.override
    def natural_width(cls, parameters: SeriesBandSettings) -> int:
        return parameters.width or round(parameters.height * 16 / 9)

    @classmethod
    @typing.override
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"metrics": ColumnKind.METRIC}


def plug(app: Dash) -> None:  # noqa: ARG001 -- a chart type needs nothing served beside it
    """Plugin: registers this chart type. It draws from the panel's band data, which the panel fetches for it."""
    SeriesBandChart.register(allow_override=True)
