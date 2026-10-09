"""A bar chart that compares an aggregated metric across groups (e.g. accuracy by hidden size)."""

from __future__ import annotations

import contextlib
import typing
from pathlib import Path

import dash_mantine_components as dmc
import pandas as pd
from pydantic import BaseModel, Field

from dlboard.models import ChartType, ColumnKind
from dlboard.plugins.charts._axis_label import DEFAULT_MAX_AXIS_LABEL_CHARS, MaxAxisLabelChars, fit_axis_label
from dlboard.plugins.charts._grouping import last_row_per_run
from dlboard.plugins.charts._table_style import HPARAM_COLUMN_PREFIX
from dlboard.serve import AssetKind, series_color, serve_asset

if typing.TYPE_CHECKING:
    from dash import Dash

_TOOLTIP_JS_PATH = Path(__file__).with_name("bar_chart_tooltip.js")


class BarChartSettings(BaseModel, frozen=True, extra="forbid"):
    """Bar chart settings."""

    column: str = Field(description="The metric to aggregate onto each bar.")
    x_axis: str = Field(description='The metric or hyperparameter key to group runs by, e.g. "hidden_size".')
    aggregation: typing.Literal["mean", "median", "min", "max", "sum", "count"] = Field(
        default="mean", description="How to combine runs that share the same x_axis value into one bar."
    )
    orientation: typing.Literal["horizontal", "vertical"] = Field(
        default="horizontal",
        description="horizontal draws upright bars with groups along the x-axis (the default); "
        "vertical draws sideways bars with groups along the y-axis.",
    )
    sort: typing.Literal["none", "ascending", "descending"] = Field(
        default="ascending",
        description="Order bars by x_axis value -- numerically if every group parses as a number, "
        "alphabetically otherwise. 'none' keeps groups in first-seen order.",
    )
    x_axis_type: typing.Literal["auto", "number", "category", "date"] = Field(
        default="auto",
        description='How to interpret x_axis values for sorting/parsing: "auto" detects numbers vs '
        'text (the previous behavior); "number" and "date" force numeric/timestamp parsing even when '
        'values look like text; "category" always sorts/groups them as plain text, e.g. to keep '
        '"v2" before "v10" instead of numeric order.',
    )
    height: int = 300
    width: int | None = Field(
        default=None,
        description="Overall chart width in px. Leave blank to size it automatically from height.",
    )
    max_axis_label_chars: MaxAxisLabelChars = DEFAULT_MAX_AXIS_LABEL_CHARS


def _resolve_column(dataframe: pd.DataFrame, name: str) -> str:
    """
    Resolve a settings field to the dataframe column it actually names.

    It may name a metric column directly, or a hyperparameter key -- the latter only exists in
    the dataframe under its prefixed column name (see
    `_dataframe_helpers.build_hyperparams_dataframe`).
    """
    hparam_column = f"{HPARAM_COLUMN_PREFIX}{name}"
    return hparam_column if hparam_column in dataframe.columns else name


def _sorted_groups(
    grouped: pd.DataFrame, x_col: str, x_axis_type: typing.Literal["auto", "number", "category", "date"]
) -> pd.DataFrame:
    match x_axis_type:
        case "category":
            return grouped.sort_values(x_col, key=lambda s: s.astype(str))
        case "date":
            return grouped.sort_values(x_col)
        case "number":
            return grouped.iloc[pd.to_numeric(grouped[x_col], errors="coerce").argsort()]
        case "auto":
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
        # `render_panel_charts` shares one fetched dataframe across every chart in a panel, calling
        # this once per chart -- mutating the caller's `dataframe` in place (the coercion loop
        # below does) corrupts it for whichever chart renders next.
        dataframe = dataframe.copy()
        x_col = _resolve_column(dataframe, parameters.x_axis)

        if str(dataframe[parameters.column].dtype) in ("object", "str"):
            with contextlib.suppress(ValueError, TypeError):
                dataframe[parameters.column] = pd.to_datetime(
                    dataframe[parameters.column], utc=True, format="ISO8601"
                )

        match parameters.x_axis_type:
            case "date":
                with contextlib.suppress(ValueError, TypeError):
                    dataframe[x_col] = pd.to_datetime(dataframe[x_col], utc=True, format="ISO8601")
            case "number":
                dataframe[x_col] = pd.to_numeric(dataframe[x_col], errors="coerce")
            case "category":
                dataframe[x_col] = dataframe[x_col].astype(str)
            case "auto":
                if str(dataframe[x_col].dtype) in ("object", "str"):
                    with contextlib.suppress(ValueError, TypeError):
                        dataframe[x_col] = pd.to_datetime(dataframe[x_col], utc=True, format="ISO8601")

        # Collapse each run down to its last-logged value before grouping, so a run that logs
        # `column` at every step doesn't outweigh a run that only logs it once -- every run
        # contributes exactly one value to its group's aggregation.
        per_run = last_row_per_run(dataframe.dropna(subset=[x_col, parameters.column]))
        grouped = per_run.groupby(x_col, as_index=False, sort=False).agg(
            **{parameters.column: (parameters.column, parameters.aggregation)}
        )
        if parameters.sort != "none":
            grouped = _sorted_groups(grouped, x_col, parameters.x_axis_type)
            if parameters.sort == "descending":
                grouped = grouped.iloc[::-1]
        grouped = grouped.rename(columns={x_col: parameters.x_axis})

        data = grouped.to_dict(orient="records")
        # Carried on each row (not a real plotted column) so the tooltip's labelFormatter --
        # `bar_chart_tooltip.js` -- can prefix the hovered x-value with what it actually is,
        # e.g. "hidden_size: 128" instead of a bare "128".
        for row in data:
            row["__x_axis_name__"] = parameters.x_axis

        # Recharts' `layout` (what mantine's `orientation` controls) decides which axis is the
        # category axis -- x for the default `horizontal` orientation, y for `vertical` -- so the
        # `type: "category"` override and the axis labels have to follow it too, or the flipped
        # chart ends up with a numeric category axis and swapped labels.
        value_label = (
            f"{parameters.aggregation}("
            f"{fit_axis_label(parameters.column, parameters.max_axis_label_chars - len(parameters.aggregation) - 2)})"
        )
        x_axis_props = {"type": "category"} if parameters.orientation == "horizontal" else {}
        y_axis_props = {"type": "category"} if parameters.orientation == "vertical" else {}
        x_axis_label = (
            fit_axis_label(parameters.x_axis, parameters.max_axis_label_chars)
            if parameters.orientation == "horizontal"
            else value_label
        )
        y_axis_label = (
            value_label
            if parameters.orientation == "horizontal"
            else fit_axis_label(parameters.x_axis, parameters.max_axis_label_chars)
        )

        return dmc.BarChart(
            h=parameters.height,
            data=data,  # pyright: ignore[reportArgumentType]
            dataKey=parameters.x_axis,
            series=[{"name": parameters.column, "label": parameters.column, "color": series_color(0)}],  # pyright: ignore[reportArgumentType]
            orientation=parameters.orientation,
            xAxisLabel=x_axis_label,
            yAxisLabel=y_axis_label,
            xAxisProps=x_axis_props,
            yAxisProps=y_axis_props,
            withLegend=False,
            withXAxis=True,
            withYAxis=True,
            tickLine="xy",
            # See `line_chart.py`'s matching `lineProps` -- Recharts otherwise replays a `<Bar>`'s
            # enter animation on every data-prop change, not just the first render, which reads as
            # the whole chart flashing on each live-update poll rather than just its values moving.
            barProps={"isAnimationActive": False},
            # A wider tooltip offset keeps it from sitting directly on top of the hovered bar,
            # which otherwise obscures the exact spot the reader is looking at --
            # allowEscapeViewBox lets it actually render outside the plot area at that offset
            # rather than getting clamped back inside it, which on a short chart otherwise means
            # the tooltip covers most of the visible plot regardless of the offset. Once it can
            # escape its own chart's bounds it can visually reach into a neighboring chart's area
            # too -- wrapperStyle's zIndex keeps it painted above that neighbor rather than
            # underneath it (later charts in the DOM otherwise paint on top by default).
            # labelFormatter prefixes the hovered x-value with the axis name -- see
            # `__x_axis_name__` above and `bar_chart_tooltip.js` (registered by `plug`, below).
            tooltipProps={
                "offset": 30,
                "allowEscapeViewBox": {"x": True, "y": True},
                "wrapperStyle": {"zIndex": 100},
                "labelFormatter": {"function": "barChartTooltipLabel"},
            },
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
    def natural_width(cls, parameters: BarChartSettings) -> int:
        return parameters.width or round(parameters.height * 16 / 9)

    @classmethod
    @typing.override
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"column": ColumnKind.METRIC, "x_axis": ColumnKind.GROUPING}


def plug(app: Dash) -> None:
    """
    Plugin.

    Registers this chart type, plus the `labelFormatter` its tooltip needs (see `render` and
    `bar_chart_tooltip.js`), served from this app instance only via `serve_asset`.
    """
    BarChart.register(allow_override=True)
    serve_asset(app, AssetKind.SCRIPT, _TOOLTIP_JS_PATH.name, _TOOLTIP_JS_PATH.read_bytes())
