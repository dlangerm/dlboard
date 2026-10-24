"""
A chart of how many metrics' values spread at each step of a run.

Drawn as a median line between the quartiles, inside the band the extremes make.
"""

from __future__ import annotations

import typing

import pandas as pd
import plotly.colors
from dash import dcc
from dash.development.base_component import Component
from pydantic import BaseModel, Field

from dlboard.models import (
    DISPLAY_SECTION,
    RUN_NAME_COLUMN,
    ChartType,
    ColumnKind,
    MetricColumn,
    NamePatternList,
    SeriesBand,
    SeriesBandRequest,
)

if typing.TYPE_CHECKING:
    from dash import Dash

_FILL_ALPHA: typing.Final = 0.12
"""How opaque the band of extremes and of quartiles is, against the median line drawn over it."""


class SeriesBandSettings(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """Series band settings."""

    metrics: NamePatternList = Field(default_factory=list)
    """The metrics whose values are spread: names, and patterns with a `*` (`layer_*/grad_norm`) for many at once."""
    grid: typing.Annotated[int, DISPLAY_SECTION] = Field(default=32, ge=2, le=64)
    """At most this many steps are drawn. The grid is as fine as that allows, and always ends on a run's last step."""
    runs: typing.Annotated[int, DISPLAY_SECTION] = Field(default=1, ge=1, le=3)
    """How many of the panel's newest runs are drawn. Each one costs a read of every metric the band spreads over."""
    height: typing.Annotated[int, DISPLAY_SECTION] = 300
    width: typing.Annotated[int | None, DISPLAY_SECTION] = Field(default=None)
    """Overall chart width in px. Leave blank to size it automatically from height."""


def _request(parameters: SeriesBandSettings) -> SeriesBandRequest:
    return SeriesBandRequest(
        patterns=frozenset(parameters.metrics), grid=parameters.grid, runs=parameters.runs
    )


def _bands(dataframe: pd.DataFrame, column: str) -> list[SeriesBand]:
    """The bands the panel read for this chart, in run and step order (a row without one is skipped)."""
    if column not in dataframe.columns:
        return []
    return sorted(dataframe[column].dropna(), key=lambda band: (band.run_id, band.step))


def _traces(bands: list[SeriesBand], names: dict[int, str]) -> list[dict[str, typing.Any]]:
    """Per run: the extremes, the quartiles and the median, each a trace over the run's grid steps."""
    traces: list[dict[str, typing.Any]] = []
    palette = plotly.colors.qualitative.Plotly
    for index, run_id in enumerate(dict.fromkeys(band.run_id for band in bands)):
        rows = [band for band in bands if band.run_id == run_id]
        steps = [band.step for band in rows]
        red, green, blue = plotly.colors.hex_to_rgb(palette[index % len(palette)])
        fill = f"rgba({red},{green},{blue},{_FILL_ALPHA})"
        name = names.get(run_id, str(run_id))
        common = {"x": steps, "type": "scatter", "mode": "lines", "legendgroup": name, "showlegend": False}
        traces.append(common | {"y": [band.minimum for band in rows], "line": {"width": 0.5}, "name": name})
        traces.append(
            common
            | {
                "y": [band.maximum for band in rows],
                "line": {"width": 0.5},
                "fill": "tonexty",
                "fillcolor": fill,
            }
        )
        traces.append(common | {"y": [band.p25 for band in rows], "line": {"width": 0}})
        traces.append(
            common
            | {"y": [band.p75 for band in rows], "line": {"width": 0}, "fill": "tonexty", "fillcolor": fill}
        )
        traces.append(
            common
            | {
                "y": [band.median for band in rows],
                "line": {"width": 2, "color": f"rgb({red},{green},{blue})"},
                "name": name,
                "showlegend": True,
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
            "data": _traces(bands, names),
            "layout": {
                "height": parameters.height,
                "margin": {"l": 50, "r": 20, "t": 20, "b": 40},
                "xaxis": {"title": {"text": "step"}},
                "yaxis": {"zeroline": False},
                "hovermode": "x unified",
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
    def hint_required_series(cls, parameters: SeriesBandSettings) -> SeriesBandRequest | None:
        return _request(parameters) if parameters.metrics else None

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
