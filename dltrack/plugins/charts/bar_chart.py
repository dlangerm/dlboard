"""A bar chart that compares an aggregated metric across groups (e.g. accuracy by hidden size)."""

from __future__ import annotations

import contextlib
import typing
from pathlib import Path

import dash_mantine_components as dmc
import pandas as pd
from flask import Response
from pydantic import BaseModel

from dltrack.models import ChartType, ColumnKind
from dltrack.plugins.charts._grouping import last_row_per_run
from dltrack.plugins.charts._table_style import HPARAM_COLUMN_PREFIX

if typing.TYPE_CHECKING:
    from dash import Dash

_TOOLTIP_JS_PATH = Path(__file__).with_name("bar_chart_tooltip.js")
_TOOLTIP_JS_ROUTE = "bar-chart-tooltip.js"

_DEFAULT_BAR_COLOR = "blue.6"


class BarChartSettings(BaseModel, frozen=True, extra="forbid"):
    """Bar chart settings."""

    column: str
    """The metric to aggregate onto each bar."""
    x_axis: str
    """The metric or hyperparameter key to group runs by, e.g. "hidden_size"."""
    aggregation: typing.Literal["mean", "median", "min", "max", "sum", "count"] = "mean"
    """How to combine runs that share the same `x_axis` value into one bar."""
    sort: bool = True
    """Order bars ascending by `x_axis` value -- numerically if every group parses as a number,
    alphabetically otherwise. Disable to keep groups in first-seen order."""
    height: int = 300


def _resolve_column(dataframe: pd.DataFrame, name: str) -> str:
    """
    Resolve a settings field to the dataframe column it actually names.

    It may name a metric column directly, or a hyperparameter key -- the latter only exists in
    the dataframe under its prefixed column name (see
    `_dataframe_helpers.build_hyperparams_dataframe`).
    """
    hparam_column = f"{HPARAM_COLUMN_PREFIX}{name}"
    return hparam_column if hparam_column in dataframe.columns else name


def _sorted_groups(grouped: pd.DataFrame, x_col: str) -> pd.DataFrame:
    numeric_x = pd.to_numeric(grouped[x_col], errors="coerce")
    if numeric_x.notna().all():
        return grouped.iloc[numeric_x.argsort()]
    return grouped.sort_values(x_col, key=lambda s: s.astype(str))


class BarChart(ChartType[BarChartSettings, pd.DataFrame, dmc.BarChart], frozen=True, extra="forbid"):
    """A bar chart comparing an aggregated metric across groups of a metric or hyperparameter."""

    name: typing.ClassVar[str] = "bar"

    @typing.override
    @classmethod
    def parameter_type(cls) -> type[BarChartSettings]:
        return BarChartSettings

    @classmethod
    @typing.override
    def render(cls, parameters: BarChartSettings, dataframe: pd.DataFrame) -> dmc.BarChart:
        x_col = _resolve_column(dataframe, parameters.x_axis)

        for c in (x_col, parameters.column):
            if str(dataframe[c].dtype) in ("object", "str"):
                with contextlib.suppress(ValueError, TypeError):
                    dataframe[c] = pd.to_datetime(dataframe[c], utc=True, format="ISO8601")

        # Collapse each run down to its last-logged value before grouping, so a run that logs
        # `column` at every step doesn't outweigh a run that only logs it once -- every run
        # contributes exactly one value to its group's aggregation.
        per_run = last_row_per_run(dataframe.dropna(subset=[x_col, parameters.column]))
        grouped = per_run.groupby(x_col, as_index=False, sort=False).agg(
            **{parameters.column: (parameters.column, parameters.aggregation)}
        )
        if parameters.sort:
            grouped = _sorted_groups(grouped, x_col)
        grouped = grouped.rename(columns={x_col: parameters.x_axis})

        data = grouped.to_dict(orient="records")
        # Carried on each row (not a real plotted column) so the tooltip's labelFormatter --
        # `bar_chart_tooltip.js` -- can prefix the hovered x-value with what it actually is,
        # e.g. "hidden_size: 128" instead of a bare "128".
        for row in data:
            row["__x_axis_name__"] = parameters.x_axis

        return dmc.BarChart(
            h=parameters.height,
            data=data,  # pyright: ignore[reportArgumentType]
            dataKey=parameters.x_axis,
            series=[{"name": parameters.column, "label": parameters.column, "color": _DEFAULT_BAR_COLOR}],  # pyright: ignore[reportArgumentType]
            xAxisLabel=parameters.x_axis,
            yAxisLabel=f"{parameters.aggregation}({parameters.column})",
            xAxisProps={"type": "category"},
            withLegend=False,
            withXAxis=True,
            withYAxis=True,
            tickLine="xy",
            # A wider tooltip offset keeps it from sitting directly on top of the hovered bar,
            # which otherwise obscures the exact spot the reader is looking at. labelFormatter
            # prefixes the hovered x-value with the axis name -- see `__x_axis_name__` above and
            # `bar_chart_tooltip.js` (registered by `plug`, below).
            tooltipProps={"offset": 30, "labelFormatter": {"function": "barChartTooltipLabel"}},
            # syncMethod="value" matches synced charts (e.g. accuracy-by-hidden-size next to
            # loss-by-hidden-size) by x-axis value rather than array index, so their tooltips
            # line up even if one chart is missing a group the other has.
            #
            # Recharts' default left margin is too tight for a rotated y-axis label plus its
            # ticks, so the label crowds the plot area (and the next chart over, once several sit
            # side by side); widen it and give the other edges matching breathing room.
            barChartProps={
                "syncId": parameters.x_axis,
                "syncMethod": "value",
                "margin": {"left": 20, "right": 20, "top": 10, "bottom": 10},
            },
        )

    @classmethod
    @typing.override
    def hint_required_columns(cls, parameters: BarChartSettings) -> set[str] | None:
        return {parameters.column, parameters.x_axis}

    @classmethod
    @typing.override
    def hint_required_artifact_keys(cls, parameters: BarChartSettings) -> set[str]:
        return set()

    @classmethod
    @typing.override
    def hint_required_hparams(cls, parameters: BarChartSettings) -> set[str]:
        # x_axis may be a metric column or a hyperparameter key -- we don't know which until the
        # data comes back, so hint both; fetching a key that isn't a real hyperparameter is a no-op.
        return {parameters.x_axis}

    @classmethod
    @typing.override
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"column": ColumnKind.METRIC, "x_axis": ColumnKind.GROUPING}


def _serve_tooltip_js() -> Response:
    return Response(_TOOLTIP_JS_PATH.read_text(), mimetype="application/javascript")


def plug(app: Dash) -> None:
    """
    Plugin.

    Registers this chart type, plus the `labelFormatter` its tooltip needs (see `render` and
    `bar_chart_tooltip.js`) -- served from a route this plugin owns and appended to this app
    instance's own script list (`app.scripts`), not Dash's global `hooks.script`/`hooks.route`
    registry, which -- like `basic_rest_backend.plug` -- would otherwise leak across every `Dash`
    app built in the same process, not just this one.
    """
    BarChart.register(allow_override=True)
    prefix = str(app.config.routes_pathname_prefix)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    route = f"{prefix}{_TOOLTIP_JS_ROUTE}"
    app.server.add_url_rule(route, endpoint=route, view_func=_serve_tooltip_js)
    app.scripts.append_script({"external_url": route, "external_only": True})  # pyright: ignore[reportUnknownMemberType]
