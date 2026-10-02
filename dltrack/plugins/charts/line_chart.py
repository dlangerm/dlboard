"""A line chart."""

from __future__ import annotations

import contextlib
import typing
from pathlib import Path

import dash_mantine_components as dmc
import pandas as pd
from pydantic import BaseModel, Field

from dltrack.models import ChartType, ColumnKind, MetricColumn
from dltrack.plugins.charts._sampling import DEFAULT_MAX_POINTS, shared_sample_grid
from dltrack.serve import AssetKind, series_color, serve_asset

if typing.TYPE_CHECKING:
    from dash import Dash

_BOOKKEEPING_COLS = frozenset({MetricColumn.RUN_ID, MetricColumn.TIMESTAMP_UTC})

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
    for run_id, run_df in df.groupby("run_id"):
        run_df = run_df[[x_axis, column]].sort_values(x_axis)
        run_df["_source_x"] = run_df[x_axis]
        filled = pd.merge_asof(all_x, run_df, on=x_axis, direction="nearest")
        in_range = all_x[x_axis].between(run_df[x_axis].iloc[0], run_df[x_axis].iloc[-1])
        logged_here = filled["_source_x"] == all_x[x_axis]
        result[str(run_id)] = filled[column].where(logged_here)
        result[f"{run_id}__y"] = filled[column].where(in_range)
        result[f"{run_id}__x"] = filled["_source_x"].where(in_range)
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
        else pd.to_datetime(column, utc=True, errors="coerce")
    )
    if as_datetime.dt.tz is None:
        as_datetime = as_datetime.dt.tz_localize("UTC")
    return (as_datetime - pd.Timestamp("1970-01-01", tz="UTC")) / pd.Timedelta(milliseconds=1)


class LineChartSettings(BaseModel, frozen=True, extra="forbid"):
    """Line chart settings."""

    column: str
    x_axis: str
    height: int = 300
    width: int | None = Field(
        default=None,
        description="Overall chart width in px. Leave blank to size it automatically from height.",
    )
    sample: bool = Field(
        default=True, description="Downsample each run's series (LTTB) so huge series stay smooth to render."
    )
    max_points: int = Field(
        default=DEFAULT_MAX_POINTS, description="Target point count per run when sample is enabled."
    )
    x_axis_type: typing.Literal["number", "category", "date", "time"] = Field(
        default="number",
        description='"number" spaces points by their actual value (step 10 sits 10x as far from 0 as '
        'step 1); "category" gives every distinct x value equal spacing regardless of its size; '
        '"date" treats the axis as a timestamp (e.g. timestamp_utc), showing real calendar dates; '
        '"time" treats it as an elapsed duration in seconds, showing it as h:mm:ss.',
    )


class LineChart(ChartType[LineChartSettings, pd.DataFrame, dmc.LineChart], frozen=True, extra="forbid"):
    """a line chart."""

    name: typing.ClassVar[str] = "line"

    @typing.override
    @classmethod
    def parameter_type(cls) -> type[LineChartSettings]:
        return LineChartSettings

    @classmethod
    @typing.override
    def render(cls, parameters: LineChartSettings, dataframe: pd.DataFrame) -> dmc.LineChart:
        # `render_panel_charts` shares one fetched dataframe across every chart in a panel, calling
        # this once per chart -- mutating the caller's `dataframe` in place (the coercion loop and
        # `_to_epoch_millis` below both used to) corrupts it for whichever chart renders next.
        dataframe = dataframe.copy()
        x_col = parameters.x_axis
        axis_cols = ["run_id", "step"]
        if x_col != "step":
            axis_cols.append(x_col)

        for c in dataframe.columns:
            if str(dataframe[c].dtype) in ("object", "str"):
                with contextlib.suppress(ValueError, TypeError):
                    dataframe[c] = pd.to_datetime(dataframe[c], utc=True, format="ISO8601")

        if parameters.x_axis_type == "date" and x_col in dataframe.columns:
            dataframe[x_col] = _to_epoch_millis(dataframe[x_col])

        axis_df = dataframe.loc[dataframe[x_col].notna(), axis_cols]
        value_df = dataframe.loc[dataframe[parameters.column].notna(), ["run_id", "step", parameters.column]]

        df = axis_df.merge(value_df, on=["run_id", "step"], how="inner")
        df = df.groupby([parameters.x_axis, "run_id"], as_index=False)[[parameters.column]].mean()
        if parameters.sample:
            # Sample against every sibling metric column sharing this x-axis, not just this
            # chart's own -- so every chart in the panel picks the same x-values and stays
            # aligned when synced, regardless of which metric each one plots.
            value_cols = sorted(
                {
                    c
                    for c in dataframe.columns
                    if c not in _BOOKKEEPING_COLS
                    and c != x_col
                    and pd.api.types.is_numeric_dtype(dataframe[c])
                }
                | {parameters.column}
            )
            wide = (
                dataframe.loc[dataframe[x_col].notna(), ["run_id", x_col, *value_cols]]
                .groupby(["run_id", x_col], as_index=False)
                .mean()
            )
            grid = shared_sample_grid(
                wide, x_col=x_col, group_col="run_id", value_cols=value_cols, max_points=parameters.max_points
            )
            df = df.merge(grid, on=["run_id", parameters.x_axis], how="inner")
        run_ids = sorted(df["run_id"].unique())
        df = _nearest_fill_pivot(df, x_axis=parameters.x_axis, column=parameters.column)
        data = df.to_dict(orient="records")
        series = [
            {"name": str(run_id), "label": f"Run {run_id}", "color": series_color(int(run_id))}
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

        return dmc.LineChart(
            h=parameters.height,
            data=data,  # pyright: ignore[reportArgumentType]
            dataKey=str(parameters.x_axis),
            series=series,  # pyright: ignore[reportArgumentType]
            xAxisLabel=f"{parameters.x_axis}",
            yAxisLabel=f"{parameters.column}",
            xAxisProps=x_axis_props,
            withLegend=True,
            withXAxis=True,
            withYAxis=True,
            withDots=False,
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
                "series": series,
            },
            # syncMethod="value" matches synced charts by x-axis value rather than
            # array index — needed because sampled/unsampled charts (or charts
            # sampled at different rates) don't share row counts, so index-based
            # sync (Recharts' default) lines up the wrong points across charts.
            #
            # Recharts' default left margin is too tight for a rotated y-axis label plus its
            # ticks, so the label crowds the plot area (and the next chart over, once several
            # sit side by side); widen it and give the other edges matching breathing room.
            lineChartProps={
                "syncId": parameters.x_axis,
                "syncMethod": "value",
                "margin": {"left": 20, "right": 20, "top": 10, "bottom": 10},
            },
        )

    @classmethod
    @typing.override
    def hint_required_columns(cls, parameters: LineChartSettings) -> set[str] | None:
        return {
            parameters.column,
            parameters.x_axis,
        }

    @classmethod
    @typing.override
    def hint_required_artifact_keys(cls, parameters: LineChartSettings) -> set[str]:
        return set()

    @classmethod
    @typing.override
    def hint_required_hparams(cls, parameters: LineChartSettings) -> set[str]:
        return set()

    @classmethod
    @typing.override
    def natural_width(cls, parameters: LineChartSettings) -> int:
        return parameters.width or round(parameters.height * 16 / 9)

    @classmethod
    @typing.override
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"column": ColumnKind.METRIC, "x_axis": ColumnKind.METRIC}


def plug(app: Dash) -> None:
    """
    Plugin.

    Registers this chart type, plus the script and styles its tooltip needs (see `render` and
    `line_chart_tooltip.js`), served from this app instance only via `serve_asset`.
    """
    LineChart.register(allow_override=True)
    serve_asset(app, AssetKind.SCRIPT, _TOOLTIP_JS_PATH.name, _TOOLTIP_JS_PATH.read_bytes())
    serve_asset(app, AssetKind.STYLESHEET, _TOOLTIP_CSS_PATH.name, _TOOLTIP_CSS_PATH.read_bytes())
