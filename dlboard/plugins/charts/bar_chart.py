"""A bar chart that compares an aggregated metric across groups (e.g. accuracy by hidden size)."""

from __future__ import annotations

import contextlib
import typing
from collections import Counter
from pathlib import Path

import dash_mantine_components as dmc
import pandas as pd
from pydantic import BaseModel, Field

from dlboard.models import (
    DISPLAY_SECTION,
    HPARAM_COLUMN_PREFIX,
    RUN_NAME_COLUMN,
    ChartType,
    ColumnKind,
    DataRequest,
    MetricColumn,
    MetricReduction,
    MetricReductions,
)
from dlboard.plugins.charts._axis_label import (
    DEFAULT_MAX_AXIS_LABEL_CHARS,
    VALUE_AXIS_WIDTH,
    MaxAxisLabelChars,
    fit_axis_label,
)
from dlboard.plugins.charts._chart_title import titled
from dlboard.plugins.charts._empty_chart import nothing_logged
from dlboard.plugins.charts._grouping import latest_per_run
from dlboard.serve import AssetKind, series_color, serve_asset

if typing.TYPE_CHECKING:
    from dash import Dash

_TOOLTIP_JS_PATH = Path(__file__).with_name("bar_chart_tooltip.js")


class BarChartSettings(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """Bar chart settings."""

    column: str
    """The metric to aggregate onto each bar."""
    x_axis: str
    """The metric or hyperparameter key to group runs by, e.g. "hidden_size"."""
    aggregation: typing.Literal["mean", "median", "min", "max", "sum", "count"] = Field(default="mean")
    """How to combine runs that share the same x_axis value into one bar."""
    orientation: typing.Annotated[typing.Literal["horizontal", "vertical"], DISPLAY_SECTION] = Field(
        default="horizontal"
    )
    """horizontal draws upright bars with groups along the x-axis (the default); vertical draws sideways bars with groups along the y-axis."""
    sort: typing.Annotated[typing.Literal["none", "ascending", "descending"], DISPLAY_SECTION] = Field(
        default="ascending"
    )
    """Order bars by x_axis value -- numerically if every group parses as a number, alphabetically otherwise. 'none' keeps groups in first-seen order."""
    x_axis_type: typing.Annotated[typing.Literal["auto", "number", "category", "date"], DISPLAY_SECTION] = (
        Field(default="auto")
    )
    """How to interpret x_axis values for sorting/parsing: "auto" detects numbers vs text (the previous behavior); "number" and "date" force numeric/timestamp parsing even when values look like text; "category" always sorts/groups them as plain text, e.g. to keep "v2" before "v10" instead of numeric order."""
    height: typing.Annotated[int, DISPLAY_SECTION] = 300
    width: typing.Annotated[int | None, DISPLAY_SECTION] = Field(default=None)
    """Overall chart width in px. Leave blank to size it automatically from height."""
    max_axis_label_chars: typing.Annotated[
        MaxAxisLabelChars, DISPLAY_SECTION, Field(title="Max axis label characters")
    ] = DEFAULT_MAX_AXIS_LABEL_CHARS
    """Longest axis title, in characters. A longer metric name is shortened to its last characters behind an ellipsis, so it stays inside the chart; raise it for a taller or wider chart."""


def _resolve_column(dataframe: pd.DataFrame, name: str) -> str:
    """
    Resolve a settings field to the dataframe column it actually names.

    It may name a metric column directly, or a hyperparameter key -- the latter only exists in
    the dataframe under its prefixed column name (see
    `_panel_data.build_hyperparams_dataframe`).
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


def _bars(per_run: pd.DataFrame, x_col: str, parameters: BarChartSettings, *, by_run: bool) -> pd.DataFrame:
    """The bars, sorted as `parameters` say: a run's own each (`by_run`), else the runs that share an x value, aggregated."""
    bars = (
        per_run
        if by_run
        else per_run.groupby(x_col, as_index=False, sort=False).agg(
            **{parameters.column: (parameters.column, parameters.aggregation)}
        )
    )
    if parameters.sort != "none":
        bars = _sorted_groups(bars, x_col, parameters.x_axis_type)
        if parameters.sort == "descending":
            bars = bars.iloc[::-1]
    return bars


def _is_named_run_axis(x_col: str, dataframe: pd.DataFrame) -> bool:
    """Whether the bars are the runs, which `dataframe` has the names of."""
    return x_col == MetricColumn.RUN_ID and RUN_NAME_COLUMN in dataframe.columns


def _named_runs(grouped: pd.DataFrame, x_col: str, dataframe: pd.DataFrame) -> pd.DataFrame:
    """
    `grouped` with its runs (if they are its bars) by their names, as every other chart names its runs.

    A run with none is "Run <id>". Two runs with one name stay two bars: each is told apart by its id, or they would
    share a tick.
    """
    if not _is_named_run_axis(x_col, dataframe):
        return grouped
    return grouped.assign(**{x_col: _run_labels(grouped[x_col], dataframe)})


def _run_labels(run_ids: pd.Series[typing.Any], dataframe: pd.DataFrame) -> list[str]:
    """Each of `run_ids` by its name in `dataframe` (else "Run <id>"), by name and id where two share a name."""
    names: dict[str, typing.Any] = {}
    if RUN_NAME_COLUMN in dataframe.columns:
        named = dataframe.drop_duplicates(MetricColumn.RUN_ID)
        # By text, since a chart whose x-axis type is "category" has its ids as text.
        names = dict(zip(named[MetricColumn.RUN_ID].astype(str), named[RUN_NAME_COLUMN], strict=True))
    labels = [
        name if isinstance(name := names.get(str(run_id)), str) and name else f"Run {run_id}"
        for run_id in run_ids
    ]
    taken = Counter(labels)
    return [
        f"{label} ({run_id})" if taken[label] > 1 else label
        for label, run_id in zip(labels, run_ids, strict=True)
    ]


def _coerce_x_axis(dataframe: pd.DataFrame, x_col: str, x_axis_type: str) -> None:
    """Read the x-axis column as the type the chart was set to, in place: `dataframe` is the chart's own copy."""
    match x_axis_type:
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


class BarChart(ChartType[BarChartSettings, pd.DataFrame, dmc.Stack], frozen=True, extra="forbid"):
    """A bar chart comparing an aggregated metric across groups of a metric or hyperparameter."""

    name: typing.ClassVar[str] = "bar"

    @typing.override
    @classmethod
    def parameter_type(cls) -> type[BarChartSettings]:
        return BarChartSettings

    @classmethod
    @typing.override
    def render(cls, parameters: BarChartSettings, dataframe: pd.DataFrame) -> dmc.Stack:
        # `dataframe` is the caller's (a chart's own frame, but a test or a plugin may share one): mutating it in
        # place (the coercion loop below does) would corrupt it for whatever reads it next.
        if dataframe.empty:
            return nothing_logged(parameters.column)
        dataframe = dataframe.copy()
        x_col = _resolve_column(dataframe, parameters.x_axis)

        if str(dataframe[parameters.column].dtype) in ("object", "str"):
            with contextlib.suppress(ValueError, TypeError):
                dataframe[parameters.column] = pd.to_datetime(
                    dataframe[parameters.column], utc=True, format="ISO8601"
                )

        _coerce_x_axis(dataframe, x_col, parameters.x_axis_type)

        # Collapse each run down to its last-logged value before grouping, so a run that logs
        # `column` at every step doesn't outweigh a run that only logs it once -- every run
        # contributes exactly one value to its group's aggregation.
        per_run = latest_per_run(dataframe, [x_col, parameters.column])
        # Runs are grouped by a hyperparameter's value ("accuracy by hidden size"). Over a metric, two runs often end on
        # one value (a schedule that decays to the same learning rate), and grouping them would collapse the chart to a
        # bar or two: each run has its own bar there, at its own value, which its tooltip says (#422).
        by_run = x_col == parameters.x_axis and x_col != MetricColumn.RUN_ID
        grouped = _bars(per_run, x_col, parameters, by_run=by_run)
        details = [f"{parameters.x_axis}: {value}" for value in grouped[x_col]] if by_run else None
        if by_run:
            grouped = grouped.assign(**{x_col: _run_labels(grouped[MetricColumn.RUN_ID], dataframe)})
        # A bar per run says which run it is by its name, not its id.
        x_title = "run" if by_run or _is_named_run_axis(x_col, dataframe) else parameters.x_axis
        grouped = _named_runs(grouped, x_col, dataframe).rename(columns={x_col: parameters.x_axis})

        data = grouped[[parameters.x_axis, parameters.column]].to_dict(orient="records")
        # Carried on each row (not a real plotted column) so the tooltip's labelFormatter --
        # `bar_chart_tooltip.js` -- can prefix the hovered x-value with what it actually is,
        # e.g. "hidden_size: 128" instead of a bare "128", and a run's bar with its own x value.
        for i, row in enumerate(data):
            row["__x_axis_name__"] = x_title
            if details is not None:
                row["__x_detail__"] = details[i]

        # Recharts' `layout` (what mantine's `orientation` controls) decides which axis is the
        # category axis -- x for the default `horizontal` orientation, y for `vertical` -- so the
        # `type: "category"` override and the axis labels have to follow it too, or the flipped
        # chart ends up with a numeric category axis and swapped labels.
        value_label = (
            f"{parameters.aggregation}("
            f"{fit_axis_label(parameters.column, parameters.max_axis_label_chars - len(parameters.aggregation) - 2)})"
        )
        x_axis_props: dict[str, typing.Any] = (
            {"type": "category"} if parameters.orientation == "horizontal" else {}
        )
        y_axis_props: dict[str, typing.Any] = (
            {"type": "category"}
            if parameters.orientation == "vertical"
            else {"width": VALUE_AXIS_WIDTH, "tickFormatter": {"function": "barChartValueTick"}}
        )
        x_axis_label = (
            fit_axis_label(x_title, parameters.max_axis_label_chars)
            if parameters.orientation == "horizontal"
            else value_label
        )

        plot = dmc.BarChart(
            h=parameters.height,
            data=data,  # pyrefly: ignore [bad-argument-type]
            dataKey=parameters.x_axis,
            series=[
                {
                    "name": parameters.column,
                    "label": parameters.column,
                    # pyrefly: ignore [bad-assignment]
                    "color": series_color(0),
                }
            ],
            orientation=parameters.orientation,
            xAxisLabel=x_axis_label,
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
            # Recharts' default margins are too tight for the ticks at the edges, which crowd the plot area
            # (and the next chart over, once several sit side by side); give every edge breathing room.
            barChartProps={
                "syncId": parameters.x_axis,
                "syncMethod": "value",
                "margin": {"left": 10, "right": 20, "top": 10, "bottom": 10},
            },
        )
        return titled(f"{parameters.aggregation}({parameters.column})", plot)

    @classmethod
    @typing.override
    def data_request(cls, parameters: BarChartSettings) -> DataRequest:
        # A bar is a run's latest value: the chart takes that from each run's series itself, so it needs no more.
        # x_axis may be a metric or a hyperparameter -- which, the data says when it comes back, so it is asked for as
        # both; asking for a key that isn't a real hyperparameter reads nothing.
        return DataRequest(
            metrics=frozenset({parameters.column, parameters.x_axis}),
            reductions=MetricReductions(default=MetricReduction.LATEST),
            hparams=frozenset({parameters.x_axis}),
        )

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
