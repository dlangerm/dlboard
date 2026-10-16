"""
A chart of logged histograms: how a distribution of values (gradient norms, weights, per-sample losses) moves over steps.

Three views of the same data, each a row per run in one Plotly figure. A run's name labels its row, with the run's
color beside it: Plotly cannot use the app's theme colors (CSS variables) in a figure, so the labels are HTML laid
over the rows rather than part of it.

- **heatmap**: step across, value bucket up, color the share of that step's values in the bucket. The "3D" view.
- **bands**: the median and the spread around it (min-max, and the middle `band_share`) per step.
- **step**: the distribution at one step as bars, with a slider to move through the steps.

Everything here is cheap whatever was logged: the store thins a run to `DLBOARD_MAX_HISTOGRAM_STEPS` steps before the
data reaches the chart, and the chart caps its rows. The values themselves were binned by the logging client.
"""

from __future__ import annotations

import bisect
import functools
import itertools
import typing
from enum import StrEnum
from pathlib import Path

import dash_mantine_components as dmc
import numpy as np
import pandas as pd
import plotly.colors
from dash import dcc, html
from plotly.subplots import make_subplots
from pydantic import BaseModel, Field, PositiveFloat, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard.models import (
    DEFAULT_RENDER_CONTEXT,
    RUN_NAME_COLUMN,
    ChartType,
    ColumnKind,
    Histogram,
    MetricColumn,
    RenderContext,
    histogram_column,
)
from dlboard.plugins.charts._chart_title import titled
from dlboard.plugins.charts._histogram_math import common_edges, regrid, spread
from dlboard.serve import AssetKind, series_swatch_class, serve_asset

if typing.TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from dash import Dash
    from dash.development.base_component import Component

_CSS_PATH: typing.Final = Path(__file__).with_name("histogram_chart.css")


class _Margin(typing.NamedTuple):
    """Space in px around the plots, for the axes and their labels."""

    left: int
    right: int
    top: int
    bottom: int


_MARGIN: typing.Final = _Margin(left=64, right=16, top=26, bottom=40)
_SLIDER_PX: typing.Final = 56
"""Room below the last row for the step view's slider."""
_LABEL_PX: typing.Final = 22
"""How far above its row a run's label sits: its own height, and a little air."""
_FADED_ALPHA: typing.Final = 0.18
_BAND_ALPHA: typing.Final = 0.4


class HistogramDisplay(StrEnum):
    """How a histogram chart draws its histograms."""

    HEATMAP = "heatmap"
    BANDS = "bands"
    STEP = "step"


class Colorscale(StrEnum):
    """The heatmap's colors, from the fewest values in a bucket to the most: Plotly's named continuous scales."""

    VIRIDIS = "Viridis"
    CIVIDIS = "Cividis"
    PLASMA = "Plasma"
    INFERNO = "Inferno"
    MAGMA = "Magma"
    BLUES = "Blues"


class HistogramChartSettings(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """Histogram chart settings."""

    key: str
    """The logged histogram to chart."""
    display: HistogramDisplay = HistogramDisplay.HEATMAP
    """`heatmap`: value buckets against step, shaded by how much of the step's values fall in each. `bands`: the median and its spread per step. `step`: the distribution at one step, with a slider to move between steps."""
    normalize: bool = True
    """Show each step as a share of its own values, so steps that logged different numbers of values compare. Turn off to show raw counts. Does not affect `bands`."""
    colorscale: Colorscale = Colorscale.VIRIDIS
    """`heatmap`: the colors from the fewest values in a bucket to the most."""
    band_share: float = Field(default=0.68, gt=0, lt=1)
    """`bands`: the share of a step's values the darker band holds around the median. 0.68 is where a normal distribution's mean plus and minus one standard deviation lie."""
    max_rows: PositiveInt = 128
    """The most value buckets drawn for a run: a finer lattice is merged by keeping every n-th edge. More than a chart is tall in pixels shows nothing, and what bounds the arithmetic."""
    max_slider_steps: PositiveInt = 300
    """`step`: the most steps the slider moves through, spread evenly over the run. A figure carries every step's data with it."""
    height: int = Field(default=220, ge=80)
    """Height in px of each run's row."""
    width: int | None = None
    """Overall chart width in px. Leave blank to size it automatically from height (the default)."""


class HistogramChartLayout(BaseSettings):
    """`DLBOARD_HISTOGRAM_*` environment variables for how every histogram chart is laid out and colored."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_HISTOGRAM_")

    accent_color: str = Field(default="#3b82f6", pattern=r"^#[0-9a-fA-F]{6}$")
    """The one color the bands and bars are drawn in, as `#rrggbb`: a Plotly figure cannot use the theme's CSS variables."""
    row_gap_px: PositiveInt = 52
    """Space between one run's row and the next, with room for the run's name and the x axis."""
    max_ticks: PositiveInt = 6
    """How many buckets a value axis names (by lower edge): naming all of them would not fit."""
    aspect: PositiveFloat = 2.5
    """How much wider than tall a chart is by default: its steps run across."""

    def rgba(self, alpha: float) -> str:
        """`accent_color` as a Plotly color of the given opacity."""
        red, green, blue = plotly.colors.hex_to_rgb(self.accent_color)
        return f"rgba({red}, {green}, {blue}, {alpha})"


class _Run(typing.NamedTuple):
    """One run's histograms of the charted key, in step order."""

    run_id: int
    name: str
    steps: list[int]
    histograms: list[Histogram]


class _CategoryAxis(typing.TypedDict):
    """The settings of an axis with one category per bucket."""

    type: typing.Literal["category"]
    categoryorder: typing.Literal["array"]
    categoryarray: list[str]
    tickmode: typing.Literal["array"]
    tickvals: list[str]
    ticktext: list[str]


class _Trace(typing.TypedDict, total=False):
    """A Plotly trace, as the properties these views set on a heatmap, a scatter or a bar."""

    type: typing.Literal["heatmap", "scatter", "bar"]
    xaxis: str
    yaxis: str
    x: list[int] | list[str]
    y: list[float] | list[str]
    z: list[list[float]]
    zmin: float
    zmax: float
    colorscale: str
    showscale: bool
    colorbar: dict[str, object]
    marker: dict[str, str]
    mode: str
    line: dict[str, object]
    hoverinfo: str
    fill: str
    fillcolor: str
    text: list[str]
    hovertemplate: str


class _Frame(typing.TypedDict):
    """One step of the slider view: new values for the first `traces`, and the layout (the step's name) to go with them."""

    name: str
    data: list[_Trace]
    traces: list[int]
    layout: dict[str, object]


def _runs(dataframe: pd.DataFrame, key: str, limit: int) -> tuple[list[_Run], int]:
    """The `limit` most recent runs that logged `key`, and how many runs did."""
    column = histogram_column(key)
    if column not in dataframe.columns:
        return [], 0
    logged = dataframe.loc[dataframe[column].notna()].sort_values([MetricColumn.RUN_ID, MetricColumn.STEP])
    run_ids = logged[MetricColumn.RUN_ID].unique()
    shown = logged[logged[MetricColumn.RUN_ID].isin(run_ids[-limit:])]
    runs: list[_Run] = []
    for run_id, group in shown.groupby(MetricColumn.RUN_ID):
        names = group.get(RUN_NAME_COLUMN, pd.Series(dtype=object)).dropna()
        runs.append(
            _Run(
                run_id=typing.cast("int", run_id),
                name=str(names.iloc[0]) if len(names) else f"Run {run_id}",
                steps=group[MetricColumn.STEP].tolist(),
                histograms=group[column].tolist(),
            )
        )
    return runs, len(run_ids)


def _evenly_spaced(count: int, wanted: int) -> list[int]:
    """`wanted` indexes spread evenly over `count` items, first and last included: all of them if they are fewer."""
    return np.unique(np.linspace(0, count - 1, min(count, wanted)).astype(int)).tolist()


def _bucket_labels(edges: np.ndarray) -> list[str]:
    """A name for each row between `edges`, which a category axis needs to be unique: precise enough to be."""
    spans = list(itertools.pairwise(edges.tolist()))
    for digits in (4, 17):
        labels = [f"[{low:.{digits}g}, {high:.{digits}g})" for low, high in spans]
        if len(set(labels)) == len(labels):
            break
    return labels


def _category_axis(edges: np.ndarray, labels: list[str], max_ticks: int) -> _CategoryAxis:
    """An axis of one category per row between `edges`, naming only a few of them (by lower edge)."""
    shown = _evenly_spaced(len(labels), max_ticks)
    return _CategoryAxis(
        type="category",
        categoryorder="array",
        categoryarray=labels,
        tickmode="array",
        tickvals=[labels[i] for i in shown],
        ticktext=[f"{edges[i]:.3g}" for i in shown],
    )


def _figure_height(runs: int, settings: HistogramChartSettings, layout: HistogramChartLayout) -> int:
    """The figure's height in px: its rows and the gaps between them, the margins, and room for a slider."""
    slider = _SLIDER_PX if settings.display is HistogramDisplay.STEP else 0
    rows = runs * settings.height + (runs - 1) * layout.row_gap_px
    return rows + _MARGIN.top + _MARGIN.bottom + slider


def _run_labels(
    runs: list[_Run], settings: HistogramChartSettings, layout: HistogramChartLayout
) -> list[Component]:
    """Each run's name and color, above its row: HTML at the row's position in the figure (see the module docstring)."""
    return [
        html.Div(
            run.name,
            className=f"dl-histogram-run {series_swatch_class(run.run_id)}",
            style={
                "left": f"{_MARGIN.left}px",
                "top": f"{_MARGIN.top + row * (settings.height + layout.row_gap_px) - _LABEL_PX}px",
            },
        )
        for row, run in enumerate(runs)
    ]


def _axis_id(letter: typing.Literal["x", "y"], row: int) -> str:
    """The name Plotly gives the `letter` axis of row `row`: `x`, `x2`, `x3`, ..."""
    return f"{letter}{row if row > 1 else ''}"


def _plot(
    runs: list[_Run],
    settings: HistogramChartSettings,
    layout: HistogramChartLayout,
    traces: list[_Trace],
    axes: Sequence[tuple[Mapping[str, object], Mapping[str, object]]],
) -> dict[str, typing.Any]:
    """
    The figure Plotly draws: a row per run, each with its axes as given, around `traces`.

    The traces and axes are plain data, not Plotly's own objects: those validate every property on the way in,
    which costs thousands of times a dict for a figure with a trace per run and a frame per step. Plotly only
    works out where the rows go. With a slider the rows show a distribution each; otherwise their x axes are the
    step, so they move together and are titled once, under the last row.
    """
    slider = settings.display is HistogramDisplay.STEP
    height = _figure_height(len(runs), settings, layout)
    rows_px = height - _MARGIN.top - _MARGIN.bottom - (_SLIDER_PX if slider else 0)
    rows = make_subplots(
        rows=len(runs),
        cols=1,
        vertical_spacing=layout.row_gap_px / rows_px,
    )
    figure: dict[str, typing.Any] = rows.to_plotly_json()["layout"]
    for row, (x_axis, y_axis) in enumerate(axes, start=1):
        figure[f"xaxis{row if row > 1 else ''}"].update(x_axis)
        figure[f"yaxis{row if row > 1 else ''}"].update(y_axis)
    if not slider:
        for row in range(2, len(runs) + 1):
            figure[f"xaxis{row}"]["matches"] = "x"
        figure[f"xaxis{len(runs) if len(runs) > 1 else ''}"]["title"] = {"text": "step"}
    figure |= {
        "height": height,
        "margin": {
            "l": _MARGIN.left,
            "r": _MARGIN.right,
            "t": _MARGIN.top,
            "b": _MARGIN.bottom + (_SLIDER_PX if slider else 0),
        },
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "showlegend": False,
        "font": {"size": 11},
        "bargap": 0,
        "hovermode": "x",
    }
    return {"data": traces, "layout": figure, "frames": []}


def _heatmap(
    runs: list[_Run], settings: HistogramChartSettings, layout: HistogramChartLayout
) -> dict[str, typing.Any]:
    grids: list[tuple[np.ndarray, np.ndarray]] = []
    for run in runs:
        edges = common_edges(run.histograms, settings.max_rows)
        counts = np.stack([regrid(h, edges) for h in run.histograms], axis=1)
        totals = counts.sum(axis=0)
        shares = counts / np.where(totals > 0, totals, 1) if settings.normalize else counts
        # An empty bucket is blank, not the lowest color.
        grids.append((edges, np.where(shares > 0, shares, np.nan)))
    peak = max((float(np.nanmax(z)) for _, z in grids if np.isfinite(z).any()), default=1.0)
    share_format = ".1%" if settings.normalize else ",.0f"
    traces: list[_Trace] = []
    axes: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    for row, (run, (edges, z)) in enumerate(zip(runs, grids, strict=True), start=1):
        labels = _bucket_labels(edges)
        traces.append(
            _Trace(
                type="heatmap",
                xaxis=_axis_id("x", row),
                yaxis=_axis_id("y", row),
                x=run.steps,
                y=labels,
                z=z.tolist(),
                zmin=0,
                zmax=peak,
                colorscale=settings.colorscale,
                showscale=row == 1,
                colorbar={"thickness": 10, "tickformat": ".0%" if settings.normalize else ",.0f"},
                hovertemplate=f"step %{{x}}<br>%{{y}}<br>%{{z:{share_format}}}"
                + (f"<extra>{run.name}</extra>" if len(runs) > 1 else "<extra></extra>"),
            )
        )
        axes.append(({"zeroline": False}, _category_axis(edges, labels, layout.max_ticks)))
    return _plot(runs, settings, layout, traces, axes)


def _bands(
    runs: list[_Run], settings: HistogramChartSettings, layout: HistogramChartLayout
) -> dict[str, typing.Any]:
    inner = (0.5 - settings.band_share / 2, 0.5, 0.5 + settings.band_share / 2)
    traces: list[_Trace] = []
    for row, run in enumerate(runs, start=1):
        spreads: list[list[float]] = np.stack([spread(h, inner) for h in run.histograms], axis=1).tolist()
        lows, near_lows, medians, near_highs, highs = spreads
        hover = [
            f"max {high:.4g}<br>{inner[2]:.0%} {near_high:.4g}<br>median {median:.4g}"
            f"<br>{inner[0]:.0%} {near_low:.4g}<br>min {low:.4g}"
            for low, near_low, median, near_high, high in zip(
                lows, near_lows, medians, near_highs, highs, strict=True
            )
        ]

        invisible = functools.partial(
            _Trace,
            type="scatter",
            xaxis=_axis_id("x", row),
            yaxis=_axis_id("y", row),
            x=run.steps,
            mode="lines",
            line={"width": 0},
            hoverinfo="skip",
        )
        # Each `tonexty` fill reaches back to the trace before it, so the order is what nests the bands.
        traces += [
            invisible(y=highs),
            invisible(y=lows, fill="tonexty", fillcolor=layout.rgba(_FADED_ALPHA)),
            invisible(y=near_highs),
            invisible(y=near_lows, fill="tonexty", fillcolor=layout.rgba(_BAND_ALPHA)),
            _Trace(
                type="scatter",
                xaxis=_axis_id("x", row),
                yaxis=_axis_id("y", row),
                x=run.steps,
                y=medians,
                mode="lines",
                line={"width": 2, "color": layout.rgba(1)},
                text=hover,
                hovertemplate="step %{x}<br>%{text}<extra></extra>",
            ),
        ]
    flat = {"zeroline": False}
    return _plot(runs, settings, layout, traces, [(flat, flat)] * len(runs))


def _bars_at(run: _Run, step: int, bars_by_histogram: list[list[float]]) -> list[float]:
    """`run`'s bars as of `step`: those of its latest histogram at or before it, none (all zero) before its first."""
    at = bisect.bisect_right(run.steps, step) - 1
    return bars_by_histogram[at] if at >= 0 else [0.0] * len(bars_by_histogram[0])


def _slider_figure(
    runs: list[_Run], settings: HistogramChartSettings, layout: HistogramChartLayout
) -> dict[str, typing.Any]:
    """The distribution of each run at one step as bars, and a slider moving them all through the steps."""
    grids = [common_edges(run.histograms, settings.max_rows) for run in runs]
    bars = [
        [
            (counts / total if settings.normalize and (total := counts.sum()) > 0 else counts).tolist()
            for counts in (regrid(h, grid) for h in run.histograms)
        ]
        for run, grid in zip(runs, grids, strict=True)
    ]
    all_steps = sorted({step for run in runs for step in run.steps})
    steps = [all_steps[i] for i in _evenly_spaced(len(all_steps), settings.max_slider_steps)]
    per_step = [
        [_bars_at(run, step, run_bars) for run, run_bars in zip(runs, bars, strict=True)] for step in steps
    ]
    peak = max((max(values) for step_bars in per_step for values in step_bars), default=1.0) or 1.0
    share_format = ".1%" if settings.normalize else ",.0f"
    traces: list[_Trace] = []
    y_axis = {"range": [0, peak * 1.05], "tickformat": ".0%" if settings.normalize else ",.0f"}
    axes: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    for row, (grid, values) in enumerate(zip(grids, per_step[-1], strict=True), start=1):
        labels = _bucket_labels(grid)
        traces.append(
            _Trace(
                type="bar",
                xaxis=_axis_id("x", row),
                yaxis=_axis_id("y", row),
                x=labels,
                y=values,
                marker={"color": layout.accent_color},
                hovertemplate=f"%{{x}}<br>%{{y:{share_format}}}<extra></extra>",
            )
        )
        axes.append((_category_axis(grid, labels, layout.max_ticks), y_axis))
    figure = _plot(runs, settings, layout, traces, axes)

    # The step each frame is at, top right above the first row (a run's name is top left, in `_run_labels`).
    stepper = {
        "xref": "paper",
        "yref": "paper",
        "x": 1,
        "y": figure["layout"]["yaxis"]["domain"][1],
        "xanchor": "right",
        "yanchor": "bottom",
        "yshift": 4,
        "showarrow": False,
        "font": {"size": 12},
    }
    annotations = [[stepper | {"text": f"step {step}"}] for step in steps]
    animate = {"mode": "immediate", "frame": {"duration": 0, "redraw": True}, "transition": {"duration": 0}}
    figure["layout"].update(
        {
            "annotations": annotations[-1],
            "sliders": [
                {
                    "active": len(steps) - 1,
                    "currentvalue": {"visible": False},
                    "pad": {"t": _SLIDER_PX - 8, "b": 0, "l": 0, "r": 0},
                    "len": 1,
                    "x": 0,
                    "y": 0,
                    "yanchor": "top",
                    # The step is named by the annotation each frame sets, so the (unreadably many) ticks need no labels.
                    "steps": [{"label": "", "method": "animate", "args": [[str(s)], animate]} for s in steps],
                }
            ],
        }
    )
    figure["frames"] = [
        _Frame(
            name=str(step),
            data=[_Trace(y=values) for values in step_bars],
            traces=list(range(len(runs))),
            layout={"annotations": notes},
        )
        for step, step_bars, notes in zip(steps, per_step, annotations, strict=True)
    ]
    return figure


class HistogramChart(ChartType[HistogramChartSettings, pd.DataFrame, dmc.Stack], frozen=True, extra="forbid"):
    """A logged histogram over steps: as a heatmap, as percentile bands, or one step at a time."""

    name: typing.ClassVar[str] = "histogram"

    @typing.override
    @classmethod
    def parameter_type(cls) -> type[HistogramChartSettings]:
        return HistogramChartSettings

    @classmethod
    @typing.override
    def render(cls, parameters: HistogramChartSettings, dataframe: pd.DataFrame) -> dmc.Stack:
        return cls.render_in(parameters, dataframe, DEFAULT_RENDER_CONTEXT)

    @classmethod
    @typing.override
    def render_in(
        cls, parameters: HistogramChartSettings, dataframe: pd.DataFrame, context: RenderContext
    ) -> dmc.Stack:
        runs, total_runs = _runs(dataframe, parameters.key, context.page_size)
        if not runs:
            return dmc.Stack([dmc.Text(f"No histograms logged for key '{parameters.key}'", c="dimmed")])
        layout = HistogramChartLayout()
        match parameters.display:
            case HistogramDisplay.HEATMAP:
                figure = _heatmap(runs, parameters, layout)
            case HistogramDisplay.BANDS:
                figure = _bands(runs, parameters, layout)
            case HistogramDisplay.STEP:
                figure = _slider_figure(runs, parameters, layout)
        plot = html.Div(
            [
                dcc.Graph(
                    figure=figure,
                    config={"displayModeBar": False, "responsive": True},
                    style={"height": f"{_figure_height(len(runs), parameters, layout)}px", "width": "100%"},
                    className="dl-histogram",
                ),
                *_run_labels(runs, parameters, layout),
            ],
            className="dl-histogram-plot",
        )
        parts: list[Component] = [plot]
        if total_runs > len(runs):
            parts.append(
                dmc.Text(f"Showing the {len(runs)} most recent of {total_runs} runs", c="dimmed", size="xs")
            )
        return titled(parameters.key, dmc.Stack(parts, gap="xs"))

    @classmethod
    @typing.override
    def hint_required_columns(cls, parameters: HistogramChartSettings) -> set[str] | None:
        return set()

    @classmethod
    @typing.override
    def hint_required_artifact_keys(cls, parameters: HistogramChartSettings) -> set[str] | None:
        return set()

    @classmethod
    @typing.override
    def hint_required_histogram_keys(cls, parameters: HistogramChartSettings) -> set[str]:
        return {parameters.key}

    @classmethod
    @typing.override
    def hint_required_hparams(cls, parameters: HistogramChartSettings) -> set[str] | None:
        return set()

    @classmethod
    @typing.override
    def natural_width(cls, parameters: HistogramChartSettings) -> int:
        return parameters.width or round(parameters.height * HistogramChartLayout().aspect)

    @classmethod
    @typing.override
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"key": ColumnKind.HISTOGRAM}


def plug(app: Dash) -> None:
    """Register the chart type, and serve the stylesheet that themes its Plotly figure."""
    HistogramChart.register(allow_override=True)
    serve_asset(app, AssetKind.STYLESHEET, _CSS_PATH.name, _CSS_PATH.read_bytes())
