"""A line chart."""

from __future__ import annotations

import contextlib
import typing
from hashlib import md5
from pathlib import Path

import dash_mantine_components as dmc
import pandas as pd
from flask import Response
from pydantic import BaseModel

from dltrack.models._view import ChartType, ColumnKind
from dltrack.plugins.charts._sampling import DEFAULT_MAX_POINTS, shared_sample_grid

if typing.TYPE_CHECKING:
    from dash import Dash

_BOOKKEEPING_COLS = frozenset({"run_id", "index", "timestamp_utc", "experiment_id"})

_TOOLTIP_JS_PATH = Path(__file__).with_name("line_chart_tooltip.js")
_TOOLTIP_JS_ROUTE = "line-chart-tooltip.js"

colors = [
    "gray",
    "red",
    "pink",
    "grape",
    "violet",
    "indigo",
    "blue",
    "cyan",
    "teal",
    "green",
    "lime",
    "yellow",
    "orange",
]


def _hash_color(run_id: int, temperature: int = 5) -> str:
    v = int(md5(str(run_id).encode(), usedforsecurity=False).hexdigest(), base=16) % len(colors)
    return colors[v] + f".{temperature % 10}"


class LineChartSettings(BaseModel, frozen=True, extra="forbid"):
    """Line chart settings."""

    column: str
    x_axis: str
    height: int = 300
    sample: bool = True
    """Downsample each run's series (LTTB) so huge series stay smooth to render."""
    max_points: int = DEFAULT_MAX_POINTS
    """Target point count per run when `sample` is enabled."""
    x_axis_type: typing.Literal["number", "category"] = "number"
    """`"number"` spaces points by their actual value (e.g. step 10 sits 10x as far from 0 as step
    1); `"category"` gives every distinct x value equal spacing regardless of its size. For a
    numeric x axis like step/epoch, `"category"` is what makes irregularly-logged points look like
    they're nonlinearly compressing — `"number"` renders it true to scale."""


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
        x_col = parameters.x_axis
        axis_cols = ["run_id", "step"]
        if x_col != "step":
            axis_cols.append(x_col)

        for c in dataframe.columns:
            if str(dataframe[c].dtype) in ("object", "str"):
                with contextlib.suppress(ValueError, TypeError):
                    dataframe[c] = pd.to_datetime(dataframe[c], utc=True, format="ISO8601")

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
        df = df.pivot(index=parameters.x_axis, columns="run_id", values=parameters.column).reset_index()
        data = df.to_dict(orient="records")
        # Carried on each row (not a real plotted column) so the tooltip's labelFormatter --
        # `line_chart_tooltip.js` -- can prefix the hovered x-value with what it actually is,
        # e.g. "step: 5" instead of a bare "5".
        for row in data:
            row["__x_axis_name__"] = parameters.x_axis
        return dmc.LineChart(
            h=parameters.height,
            data=data,  # pyright: ignore[reportArgumentType]
            dataKey=str(parameters.x_axis),
            series=[
                {
                    "name": str(col),
                    "label": f"Run {col}",
                    "color": _hash_color(int(col)),
                }
                for col in df.columns
                if col != parameters.x_axis
            ],  # pyright: ignore[reportArgumentType]
            xAxisLabel=f"{parameters.x_axis}",
            yAxisLabel=f"{parameters.column}",
            xAxisProps={"type": parameters.x_axis_type},
            withLegend=True,
            withXAxis=True,
            withYAxis=True,
            withDots=False,
            tickLine="xy",
            # A wider tooltip offset keeps it from sitting directly on top of the cursor's
            # point/line, which otherwise obscures the exact spot the reader is looking at.
            # labelFormatter prefixes the hovered x-value with the axis name -- see
            # `__x_axis_name__` above and `line_chart_tooltip.js` (registered by `plug`, below).
            tooltipProps={"offset": 30, "labelFormatter": {"function": "lineChartTooltipLabel"}},
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
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"column": ColumnKind.METRIC, "x_axis": ColumnKind.METRIC}


def _serve_tooltip_js() -> Response:
    return Response(_TOOLTIP_JS_PATH.read_text(), mimetype="application/javascript")


def plug(app: Dash) -> None:
    """
    Plugin.

    Registers this chart type, plus the `labelFormatter` its tooltip needs (see `render` and
    `line_chart_tooltip.js`) -- served from a route this plugin owns and appended to this app
    instance's own script list (`app.scripts`), not Dash's global `hooks.script`/`hooks.route`
    registry, which -- like `basic_rest_backend.plug` -- would otherwise leak across every `Dash`
    app built in the same process, not just this one.
    """
    LineChart.register()
    prefix = str(app.config.routes_pathname_prefix)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    route = f"{prefix}{_TOOLTIP_JS_ROUTE}"
    app.server.add_url_rule(route, endpoint=route, view_func=_serve_tooltip_js)
    app.scripts.append_script({"external_url": route, "external_only": True})  # pyright: ignore[reportUnknownMemberType]
