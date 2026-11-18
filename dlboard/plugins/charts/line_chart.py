"""A line chart."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import typing
from pathlib import Path

import dash_mantine_components as dmc
import pandas as pd
from pydantic import BaseModel, Field

from dlboard.models import (
    ADVANCED_SECTION,
    DISPLAY_SECTION,
    RUN_NAME_COLUMN,
    ChartType,
    ColumnKind,
    DataRequest,
    MetricColumn,
    RenderContext,
)
from dlboard.plugins.charts._axis_label import (
    DEFAULT_MAX_AXIS_LABEL_CHARS,
    VALUE_AXIS_WIDTH,
    MaxAxisLabelChars,
    fit_axis_label,
)
from dlboard.plugins.charts._chart_title import titled
from dlboard.plugins.charts._empty_chart import nothing_logged, nothing_to_place
from dlboard.plugins.charts._run_groups import grouped_traces, groups_of
from dlboard.plugins.charts._sampling import DEFAULT_MAX_POINTS, shared_sample_grid
from dlboard.plugins.charts._themed_figure import plotly_layout, register_themed_graphs, themed_graph
from dlboard.serve import AssetKind, series_color, series_dash, serve_asset

if typing.TYPE_CHECKING:
    from dash import Dash

_BOOKKEEPING_COLS = frozenset({MetricColumn.RUN_ID, MetricColumn.TIMESTAMP_UTC})

LEGEND_MAX_HEIGHT: typing.Final = 64
"""The most of a line chart's height, in px, its legend of runs takes: two rows of it, scrolling past them."""

READ_NOTE_FACTOR: typing.Final = 4
"""A chart that read more than this many times the points it can draw (`max_points`) says how many it read."""


def _read_note(read: int, drawn: int) -> dmc.Text:
    """What a chart that read far more than it draws says under itself: it is slow for what it shows."""
    return dmc.Text(
        f"{read:,} points read, {drawn:,} drawn: logging less often (Lightning's log_every_n_steps) draws this faster",
        size="xs",
        c="dimmed",
    )


_TOOLTIP_JS_PATH = Path(__file__).with_name("line_chart_tooltip.js")
_TOOLTIP_CSS_PATH = Path(__file__).with_name("line_chart_tooltip.css")

_TOOLTIP_CONTENT: typing.Final = "lineChartTooltip"
"""Draws the hover tooltip itself (`line_chart_tooltip.js`), in place of Mantine's default."""

# Recharts' XAxis has no real time/date scale (only "number"/"category") -- a "date" x-axis is
# rendered as a numeric one, epoch-milliseconds-valued, with a tick/tooltip formatter
# (`line_chart_tooltip.js`) turning that back into a calendar date client-side.
_TOOLTIP_LABEL_FORMATTER: typing.Final = {
    "date": "lineChartTooltipLabelDate",
    "time": "lineChartTooltipLabelTime",
}
_TICK_FORMATTER: typing.Final = {
    "date": "lineChartDateTick",
    "time": "lineChartTimeTick",
}


YScale = typing.Literal["linear", "log"]
"""How a line chart spaces its values: evenly, or by order of magnitude."""

_GROUPED_KIND: typing.Final = "grouped-line"
"""The kind of a grouped line chart's graph (`themed_graph`)."""

_RAW: typing.Final = "__raw"
"""The suffix of a smoothed run's logged values: its frame column, and its tooltip one (`"<run_id>__raw__y"`)."""


def _smoothed(df: pd.DataFrame, x_axis: str, column: str, weight: float) -> pd.DataFrame:
    """
    `df` with each run's `column` smoothed in x order, and what it logged kept beside it (`column + _RAW`).

    TensorBoard's debiased exponential moving average, which is pandas' `ewm(adjust=True)`: each point a weighted mean of
    the run's points up to it, the latest weighted most. Smoothed here, over every point read, before the line is
    thinned to `max_points`: smoothing the thinned points would draw a different curve at every `max_points`.
    """
    if not weight:
        return df

    def average(values: pd.Series) -> pd.Series:
        return values.ewm(alpha=1 - weight, adjust=True).mean()

    ordered = df.sort_values(["run_id", x_axis])
    return ordered.assign(
        **{f"{column}{_RAW}": ordered[column], column: ordered.groupby("run_id")[column].transform(average)}
    )


def _placeable(df: pd.DataFrame, column: str, y_scale: YScale) -> tuple[pd.DataFrame, list[dmc.Text]]:
    """The points of `df` a `y_scale` axis can place, and a note of how many it can't: a log axis has no 0 or below."""
    match y_scale:
        case "linear":
            return df, []
        case "log":
            placeable = df[column] > 0
            unplaceable = (~placeable).sum()
            note = f"{unplaceable:,} values at or below 0 left out: a log scale can't place them"
            return df[placeable], [dmc.Text(note, size="xs", c="dimmed")] if unplaceable else []


def _y_axis_props(y_scale: YScale) -> dict[str, typing.Any]:
    """The value axis: its width and tick format, and for a log scale the scale and a domain of the data's own range."""
    props: dict[str, typing.Any] = {
        "width": VALUE_AXIS_WIDTH,
        "tickFormatter": {"function": "lineChartValueTick"},
    }
    match y_scale:
        case "linear":
            return props
        case "log":
            return props | {"scale": "log", "domain": ["auto", "auto"], "allowDataOverflow": True}


def _nearest_fill_pivot(df: pd.DataFrame, x_axis: str, column: str) -> pd.DataFrame:
    """
    Pivot `df` (long: `[x_axis, "run_id", column]`) into one row per x that *any* run logged at.

    Each run gets three columns:

    - `"<run_id>"`, the plotted one: only what the run really logged at that x, empty elsewhere.
      The line is drawn straight between a run's logged points (Mantine's `connectNulls`), not
      through values it never logged.
    - `"<run_id>__y"`, the run's *nearest* logged value (`merge_asof(..., direction="nearest")`),
      for the tooltip. Runs routinely log at different x's (different step counts, different
      sampling), so going by the plotted column alone, runs would pop in and out of the tooltip
      depending on exactly which x is hovered.
    - `"<run_id>__x"`, the x that nearest value came from, so the tooltip can say where it really
      came from, rather than implying it was logged at the hovered x.

    Filling stops at a run's own first and last logged x: outside that range the run's cells stay
    empty. Otherwise a run that stopped early would show its final value out to the longest run's
    last step, as if it had kept logging it.
    """
    all_x = pd.DataFrame({x_axis: sorted(df[x_axis].unique())})
    result = {x_axis: all_x[x_axis]}
    raw = f"{column}{_RAW}"
    values = [column, *([raw] if raw in df.columns else [])]
    for run_id, run_df in df.groupby("run_id"):
        run_df = run_df[[x_axis, *values]].sort_values(x_axis)
        run_df["_source_x"] = run_df[x_axis]
        filled = pd.merge_asof(all_x, run_df, on=x_axis, direction="nearest")
        in_range = all_x[x_axis].between(run_df[x_axis].iloc[0], run_df[x_axis].iloc[-1])
        logged_here = filled["_source_x"] == all_x[x_axis]
        result[str(run_id)] = filled[column].where(logged_here)
        result[f"{run_id}__y"] = filled[column].where(in_range)
        result[f"{run_id}__x"] = filled["_source_x"].where(in_range)
        if raw in values:  # a smoothed run's logged value, named in the tooltip beside its smoothed one
            result[f"{run_id}{_RAW}__y"] = filled[raw].where(in_range)
    return pd.DataFrame(result)


def _to_epoch_millis(column: pd.Series) -> pd.Series:
    """
    Coerce `column` to a numeric epoch-milliseconds series, keeping unparseable values as NaN.

    Dividing the raw `int64` view by a fixed power of ten (the obvious approach) silently gives
    the wrong unit -- `pd.to_datetime` doesn't consistently pick nanosecond resolution (pandas 2.x
    parses ISO8601 strings as `datetime64[us]` here), so a fixed `// 10**6` divisor is only correct
    some of the time. Subtracting the epoch as a `Timestamp` and dividing by a `Timedelta` is
    resolution-agnostic, and NaT survives it as NaN for free.
    """
    as_datetime = (
        column
        if pd.api.types.is_datetime64_any_dtype(column)
        else pd.to_datetime(column, utc=True, errors="coerce", format="ISO8601")
    )
    if as_datetime.dt.tz is None:
        as_datetime = as_datetime.dt.tz_localize("UTC")
    return (as_datetime - pd.Timestamp("1970-01-01", tz="UTC")) / pd.Timedelta(milliseconds=1)


class LineChartSettings(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """Line chart settings."""

    column: str
    x_axis: str
    height: typing.Annotated[int, DISPLAY_SECTION] = 300
    width: typing.Annotated[int | None, DISPLAY_SECTION] = Field(default=None)
    """Overall chart width in px. Leave blank to size it automatically from height."""
    sample: typing.Annotated[bool, ADVANCED_SECTION] = Field(default=True)
    """Downsample each run's series (LTTB) so huge series stay smooth to render."""
    max_points: typing.Annotated[int, ADVANCED_SECTION] = Field(default=DEFAULT_MAX_POINTS)
    """Target point count per run when sample is enabled."""
    max_axis_label_chars: typing.Annotated[
        MaxAxisLabelChars, DISPLAY_SECTION, Field(title="Max axis label characters")
    ] = DEFAULT_MAX_AXIS_LABEL_CHARS
    """Longest axis title, in characters. A longer metric name is shortened to its last characters behind an ellipsis, so it stays inside the chart; raise it for a taller or wider chart."""
    x_axis_type: typing.Annotated[typing.Literal["number", "category", "date", "time"], DISPLAY_SECTION] = (
        Field(default="number")
    )
    """"number" spaces points by their actual value (step 10 sits 10x as far from 0 as step 1); "category" gives every distinct x value equal spacing regardless of its size; "date" treats the axis as a timestamp (e.g. timestamp_utc), showing real calendar dates; "time" treats it as an elapsed duration in seconds, showing it as h:mm:ss."""
    smoothing: typing.Annotated[float, DISPLAY_SECTION] = Field(default=0.0, ge=0.0, le=0.99)
    """Smooths each run's line as TensorBoard does (a debiased moving average): 0 is off, 0.9 smooths a lot. The tooltip names each logged value beside its smoothed one."""
    y_scale: typing.Annotated[YScale, DISPLAY_SECTION] = Field(default="linear")
    """"log" for values spread over orders of magnitude (a loss early and late in training). It can't place a value at or below 0: those are left out, and the chart says how many."""
    group_by: typing.Annotated[str, DISPLAY_SECTION] = Field(default="")
    """A hyperparameter (`lr`): a line per value of it, the mean of its runs in a band from the lowest to the highest. Leave blank for a line per run."""
    max_groups: typing.Annotated[int, ADVANCED_SECTION] = Field(default=12, ge=1, le=24)
    """The most groups drawn, in the order of their values: past a dozen the bands are too many to tell apart."""


@dataclasses.dataclass(frozen=True)
class _Lines:
    """The lines a chart draws, however it draws them: each run's points, nearest-filled (`_nearest_fill_pivot`)."""

    pivot: pd.DataFrame
    run_ids: list[int]
    notes: list[dmc.Text]
    single_point: bool
    """Whether a run has a single point, drawn as a dot: there is no line to draw of it."""


def _lines(parameters: LineChartSettings, dataframe: pd.DataFrame) -> _Lines | dmc.Stack:
    """The runs' points as the axes place them, smoothed and thinned; or what the chart says instead, if there are none."""
    x_col = parameters.x_axis
    x_values = dataframe[x_col]
    if str(x_values.dtype) in ("object", "str"):
        with contextlib.suppress(ValueError, TypeError):
            x_values = pd.to_datetime(x_values, utc=True, format="ISO8601")
    if parameters.x_axis_type == "date":
        x_values = _to_epoch_millis(x_values)
    has_x = x_values.notna()
    if (
        not has_x.any()
    ):  # rows, but no x the axis can place (no date, for a date axis): say so, not a blank chart
        return nothing_to_place(parameters.column, x_col)

    plotted = dataframe.loc[has_x, ["run_id", "step", parameters.column]].assign(**{x_col: x_values[has_x]})
    has_value = plotted[parameters.column].notna()
    read = has_value.sum()
    df = plotted.loc[has_value].groupby([x_col, "run_id"], as_index=False)[[parameters.column]].mean()
    df, notes = _placeable(df, parameters.column, parameters.y_scale)
    df = _smoothed(df, x_col, parameters.column, parameters.smoothing)
    if parameters.sample:
        # Sample against every sibling metric column sharing this x-axis, not just this
        # chart's own -- so every chart in the panel picks the same x-values and stays
        # aligned when synced, regardless of which metric each one plots.
        value_cols = sorted(
            {
                str(c)
                for c, dtype in dataframe.dtypes.items()
                if c not in _BOOKKEEPING_COLS and c != x_col and pd.api.types.is_numeric_dtype(dtype)
            }
            | {parameters.column}
        )
        wide = dataframe.loc[has_x, ["run_id", *value_cols]].assign(**{x_col: x_values[has_x]})
        if wide.duplicated(["run_id", x_col]).any():
            wide = wide.groupby(["run_id", x_col], as_index=False).mean()
        grid = shared_sample_grid(
            wide, x_col=x_col, group_col="run_id", value_cols=value_cols, max_points=parameters.max_points
        )
        df = df.merge(grid, on=["run_id", parameters.x_axis], how="inner")
    if df.empty:  # every value was one a log scale can't place
        return titled(parameters.column, nothing_to_place(parameters.column, x_col), *notes)
    if read > READ_NOTE_FACTOR * parameters.max_points:
        notes.append(_read_note(read, len(df)))
    return _Lines(
        pivot=_nearest_fill_pivot(df, x_axis=parameters.x_axis, column=parameters.column),
        run_ids=sorted(int(run) for run in df["run_id"].unique()),
        notes=notes,
        # A run with one point (an x-axis its values share, like a learning rate that never changes) has no line to
        # draw, so it is drawn as the dot it is.
        single_point=bool((df.groupby("run_id").size() == 1).any()),
    )


def _grouped(parameters: LineChartSettings, dataframe: pd.DataFrame, chart_id: str) -> dmc.Stack:
    """The runs grouped by `group_by`'s value: a band and a mean line per group (`_run_groups`), with Plotly."""
    lines = _lines(parameters, dataframe)
    if not isinstance(lines, _Lines):
        return lines
    groups = groups_of(dataframe, parameters.group_by)
    traces, left_out = grouped_traces(
        lines.pivot, parameters.x_axis, groups, parameters.group_by, parameters.max_groups
    )
    layout = plotly_layout(
        parameters.height,
        xaxis={
            "title": {"text": parameters.x_axis},
            "type": {"date": "date", "category": "category"}.get(parameters.x_axis_type, "linear"),
            "gridcolor": "var(--mantine-color-default-border)",
        },
        yaxis={"type": parameters.y_scale, "gridcolor": "var(--mantine-color-default-border)"},
        hovermode="x unified",
        legend={"orientation": "h", "y": -0.25},
    )
    raw = json.dumps([chart_id, parameters.model_dump()], sort_keys=True)
    instance = hashlib.shake_256(raw.encode()).hexdigest(8)
    notes = [
        dmc.Text(
            f"{len(set(groups.values()))} groups of {len(groups)} runs: a group's mean, in a band from its lowest run to its highest",
            size="xs",
            c="dimmed",
        ),
        *(
            [dmc.Text(f"{left_out} more groups not drawn: raise max_groups", size="xs", c="dimmed")]
            if left_out
            else []
        ),
        *lines.notes,
    ]
    graph = themed_graph(_GROUPED_KIND, instance, {"data": traces, "layout": layout}, parameters.height)
    return titled(parameters.column, graph, *notes)


class LineChart(ChartType[LineChartSettings, pd.DataFrame, dmc.Stack], frozen=True, extra="forbid"):
    """a line chart."""

    name: typing.ClassVar[str] = "line"

    @typing.override
    @classmethod
    def parameter_type(cls) -> type[LineChartSettings]:
        return LineChartSettings

    @classmethod
    @typing.override
    def render_in(
        cls, parameters: LineChartSettings, dataframe: pd.DataFrame, context: RenderContext
    ) -> dmc.Stack:
        """A line per run, or with `group_by` a band and a mean line per group of runs (`_run_groups`)."""
        if dataframe.empty or not parameters.group_by:
            return cls.render(parameters, dataframe)
        return _grouped(parameters, dataframe, context.chart_id)

    @classmethod
    @typing.override
    def render(cls, parameters: LineChartSettings, dataframe: pd.DataFrame) -> dmc.Stack:
        if dataframe.empty:
            return nothing_logged(parameters.column)
        # `dataframe` is the caller's (a chart's own frame, but a test or a plugin may share one), so this works
        # on the few columns it needs and never writes to it.
        # Captured before the columns below are sliced down to just the plotted ones, and before
        # `_nearest_fill_pivot` rebuilds the frame from scratch -- `run_name` doesn't survive
        # either. A run with no name in the merged dataframe (an older, pre-auto-naming
        # row -- see `RUN_NAME_COLUMN`) falls back to its bare id, same as everywhere else that
        # reads `run.name`.
        run_names: dict[typing.Any, typing.Any] = (
            dataframe.drop_duplicates("run_id").set_index("run_id")[RUN_NAME_COLUMN].to_dict()
            if RUN_NAME_COLUMN in dataframe.columns
            else {}
        )
        lines = _lines(parameters, dataframe)
        if not isinstance(lines, _Lines):
            return lines
        run_ids, notes, has_single_point_run = lines.run_ids, lines.notes, lines.single_point
        data = lines.pivot.to_dict(orient="records")
        series = [
            {
                "name": str(run_id),
                "label": run_names.get(run_id, f"Run {run_id}"),
                "color": series_color(run_id),
                "strokeDasharray": series_dash(run_id).dasharray,
            }
            for run_id in run_ids
        ]

        x_axis_props: dict[str, typing.Any] = {
            "type": "category" if parameters.x_axis_type == "category" else "number"
        }
        if parameters.x_axis_type in _TICK_FORMATTER:
            x_axis_props["tickFormatter"] = {"function": _TICK_FORMATTER[parameters.x_axis_type]}
            # Recharts' default numeric-axis domain is `[0, "auto"]`, not the data's own range --
            # for an epoch-ms "date" axis that drags the left edge all the way back to 1970,
            # squeezing every real point into a sliver at the far right. "date"/"time" values are
            # never sensibly compared against 0 the way a "number" axis's data might be, so anchor
            # to the actual data range instead.
            x_axis_props["domain"] = ["dataMin", "dataMax"]
        label_formatter = _TOOLTIP_LABEL_FORMATTER.get(parameters.x_axis_type, "lineChartTooltipLabel")

        plot = dmc.LineChart(
            h=parameters.height,
            data=data,  # pyrefly: ignore [bad-argument-type]
            dataKey=parameters.x_axis,
            series=series,  # pyrefly: ignore [bad-argument-type]
            xAxisLabel=fit_axis_label(parameters.x_axis, parameters.max_axis_label_chars),
            xAxisProps=x_axis_props,
            yAxisProps=_y_axis_props(parameters.y_scale),
            withLegend=True,
            # A legend of many runs wraps to many rows, which Recharts takes from the plot: capped at a couple of
            # rows (and scrolled past them), the plot keeps its height whatever the number of runs. The tooltip
            # still lists every run.
            legendProps={"wrapperStyle": {"maxHeight": LEGEND_MAX_HEIGHT, "overflowY": "auto"}},
            withXAxis=True,
            withYAxis=True,
            withDots=has_single_point_run,
            tickLine="xy",
            # Recharts animates a `<Line>`'s enter transition by default -- redrawing the whole
            # curve from scratch -- on *every* data-prop change, not just the first render. That
            # reads as the whole chart flashing/re-rendering each time a live-update poll delivers
            # new points, not just the line growing by one point. `lineProps` passes straight
            # through to Recharts' `Line` component, which is where this is actually controlled.
            lineProps={"isAnimationActive": False},
            # A wider tooltip offset keeps it from sitting directly on top of the cursor's
            # point/line, which otherwise obscures the exact spot the reader is looking at --
            # allowEscapeViewBox lets it actually render outside the plot area at that offset
            # rather than getting clamped back inside it, which on a short chart otherwise means
            # the tooltip covers most of the visible plot regardless of the offset. Once it can
            # escape its own chart's bounds it can visually reach into a neighboring chart's area
            # too -- wrapperStyle's zIndex keeps it painted above that neighbor rather than
            # underneath it (later charts in the DOM otherwise paint on top by default).
            # `content` draws the tooltip (see `line_chart_tooltip.js`), formatting x-values -- the
            # hovered one, and any a series' nearest-filled value really came from -- with
            # labelFormatter. Recharts hands every tooltip prop on to `content`, which is how it
            # gets the axis name and the series: it lists every run with a value near the hovered
            # x (see `_nearest_fill_pivot`), not only those Recharts' own payload has a point for.
            tooltipProps={
                "offset": 30,
                "allowEscapeViewBox": {"x": True, "y": True},
                "wrapperStyle": {"zIndex": 100},
                "content": {"function": _TOOLTIP_CONTENT},
                "labelFormatter": {"function": label_formatter},
                "xAxisName": parameters.x_axis,
                "chartTitle": parameters.column,
                "series": series,
            },
            # syncMethod="value" matches synced charts by x-axis value rather than
            # array index — needed because sampled/unsampled charts (or charts
            # sampled at different rates) don't share row counts, so index-based
            # sync (Recharts' default) lines up the wrong points across charts.
            #
            # Recharts' default margins are too tight for the ticks at the edges, which crowd the plot area
            # (and the next chart over, once several sit side by side); give every edge breathing room.
            lineChartProps={
                "syncId": parameters.x_axis,
                "syncMethod": "value",
                "margin": {"left": 10, "right": 20, "top": 10, "bottom": 10},
            },
        )
        return titled(parameters.column, plot, *notes)

    @classmethod
    @typing.override
    def data_request(cls, parameters: LineChartSettings) -> DataRequest:
        hparams = frozenset({parameters.group_by} if parameters.group_by else ())
        return DataRequest(metrics=frozenset({parameters.column, parameters.x_axis}), hparams=hparams)

    @classmethod
    @typing.override
    def natural_width(cls, parameters: LineChartSettings) -> int:
        return parameters.width or round(parameters.height * 16 / 9)

    @classmethod
    @typing.override
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"column": ColumnKind.METRIC, "x_axis": ColumnKind.METRIC, "group_by": ColumnKind.HPARAM}


def plug(app: Dash) -> None:
    """
    Plugin.

    Registers this chart type, plus the script and styles its tooltip needs (see `render` and
    `line_chart_tooltip.js`), served from this app instance only via `serve_asset`.
    """
    LineChart.register(allow_override=True)
    register_themed_graphs(app, _GROUPED_KIND)
    serve_asset(app, AssetKind.SCRIPT, _TOOLTIP_JS_PATH.name, _TOOLTIP_JS_PATH.read_bytes())
    serve_asset(app, AssetKind.STYLESHEET, _TOOLTIP_CSS_PATH.name, _TOOLTIP_CSS_PATH.read_bytes())
