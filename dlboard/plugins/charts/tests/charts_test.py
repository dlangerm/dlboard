"""Tests for LTTB downsampling and the line chart that relies on it."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import dash_mantine_components as dmc
import numpy as np
import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp
from pydantic import ValidationError

from dlboard.conftest import props
from dlboard.models import RUN_NAME_COLUMN, ChartInstance, MetricReduction
from dlboard.plugins.charts import line_chart
from dlboard.plugins.charts._axis_label import (
    DEFAULT_MAX_AXIS_LABEL_CHARS,
    MIN_MAX_AXIS_LABEL_CHARS,
    VALUE_AXIS_WIDTH,
)
from dlboard.plugins.charts._sampling import (
    MIN_MAX_POINTS,
    downsample_grouped,
    downsample_series,
    shared_sample_grid,
)
from dlboard.plugins.charts.bar_chart import BarChart, BarChartSettings
from dlboard.plugins.charts.line_chart import (
    _TICK_FORMATTER,
    _TOOLTIP_CONTENT,
    _TOOLTIP_LABEL_FORMATTER,
    LineChart,
    LineChartSettings,
)
from dlboard.serve import series_color

if TYPE_CHECKING:
    from collections.abc import Callable

    from dash.development.base_component import Component


def _plot(chart: dmc.Stack) -> Component:
    """The plot a line or bar chart renders under its title."""
    return next(node for node in chart._traverse() if isinstance(node, (dmc.LineChart, dmc.BarChart)))


def _props(chart: dmc.Stack) -> dict[str, Any]:
    return props(_plot(chart))


_YS = st.floats(-1e6, 1e6)


@given(ys=st.lists(_YS, min_size=1, max_size=200), max_points=st.integers(MIN_MAX_POINTS, 60))
def test_downsample_series_keeps_max_points_of_its_rows_in_order_with_both_ends(
    ys: list[float], max_points: int
) -> None:
    """A series within the budget is kept whole."""
    df = pd.DataFrame({"x": range(len(ys)), "y": ys})

    out = downsample_series(df, "x", "y", max_points=max_points)

    assert len(out) == min(len(df), max_points)
    assert (out["x"].iloc[0], out["x"].iloc[-1]) == (0, len(df) - 1)
    assert out["x"].is_monotonic_increasing
    pd.testing.assert_frame_equal(out, df.loc[out.index])


@given(sizes=st.lists(st.integers(1, 80), max_size=3), max_points=st.integers(MIN_MAX_POINTS, 30))
def test_downsample_grouped_downsamples_each_group_on_its_own(sizes: list[int], max_points: int) -> None:
    df = pd.DataFrame(
        [
            {"run_id": run, "x": x, "y": float((x * 7919 + run) % 101)}
            for run, size in enumerate(sizes)
            for x in range(size)
        ],
        columns=["run_id", "x", "y"],
    )

    out = downsample_grouped(df, "x", "y", "run_id", max_points=max_points)

    assert {run: list(group["x"]) for run, group in out.groupby("run_id")} == {
        run: list(downsample_series(group, "x", "y", max_points)["x"]) for run, group in df.groupby("run_id")
    }


def test_a_line_chart_samples_to_no_fewer_points_than_lttb_can_pick() -> None:
    """Fewer than the first, the last and one between left a series unsampled, the opposite of what it asked for."""
    with pytest.raises(ValidationError):
        LineChartSettings(column="loss", x_axis="step", max_points=MIN_MAX_POINTS - 1)


def test_downsample_grouped_empty_df_is_noop() -> None:
    df = pd.DataFrame(columns=["x", "y", "run_id"])
    assert downsample_grouped(df, "x", "y", "run_id", max_points=10).empty


def test_shared_sample_grid_empty_df_is_noop() -> None:
    df = pd.DataFrame(columns=["x", "y", "run_id"])
    assert shared_sample_grid(df, "x", "run_id", ["y"], max_points=10).empty


def _per_column_shared_sample_grid(
    df: pd.DataFrame, x_col: str, group_col: str, value_cols: list[str], max_points: int
) -> pd.DataFrame:
    """The straightforward per-column definition `shared_sample_grid` is a vectorized form of."""
    parts: list[pd.DataFrame] = []
    for _, g in df.sort_values([group_col, x_col]).groupby(group_col):
        selected_x: set[object] = set()
        for col in value_cols:
            sub = g.loc[g[col].notna(), [x_col, col]]
            if not sub.empty:
                selected_x.update(downsample_series(sub, x_col, col, max_points)[x_col].tolist())
        if selected_x:
            parts.append(g.loc[g[x_col].isin(selected_x), [group_col, x_col]].drop_duplicates())
    return pd.concat(parts, ignore_index=True) if parts else df.loc[:, [group_col, x_col]].drop_duplicates()


@st.composite
def _runs_of_columns(draw: st.DrawFn) -> pd.DataFrame:
    """A run or two of values in three columns, each logged at some steps: densely, sparsely, or never."""
    frames: list[pd.DataFrame] = []
    for run_id in range(draw(st.integers(1, 2))):
        n = draw(st.integers(1, 120))
        frames.append(
            pd.DataFrame(
                {
                    "x": np.arange(n),
                    "run_id": run_id,
                    **{
                        col: draw(hnp.arrays(float, n, elements=st.one_of(_YS, st.just(np.nan))))
                        for col in ("a", "b", "c")
                    },
                }
            )
        )
    df = pd.concat(frames, ignore_index=True)
    if draw(st.booleans()):  # a datetime x-axis
        df["x"] = pd.Timestamp("2026-01-01") + pd.to_timedelta(df["x"], unit="s")
    return df


@given(df=_runs_of_columns(), max_points=st.integers(MIN_MAX_POINTS, 60))
def test_shared_sample_grid_picks_what_sampling_each_column_on_its_own_would(
    df: pd.DataFrame, max_points: int
) -> None:
    """So charts of different metrics, synced by x-value, always have a point at the same place."""
    actual = shared_sample_grid(df, "x", "run_id", ["a", "b", "c"], max_points)
    expected = _per_column_shared_sample_grid(df, "x", "run_id", ["a", "b", "c"], max_points)

    pd.testing.assert_frame_equal(actual, expected)


def _metrics_df(points_per_run: int, n_runs: int = 2) -> pd.DataFrame:
    frames = [
        pd.DataFrame(
            {
                "run_id": run_id,
                "step": range(points_per_run),
                "loss": np.sin(np.linspace(0, 10, points_per_run)),
            }
        )
        for run_id in range(1, n_runs + 1)
    ]
    return pd.concat(frames, ignore_index=True)


def test_line_chart_render_sample_bounds_points() -> None:
    df = _metrics_df(2000)
    chart = LineChart.render(LineChartSettings(column="loss", x_axis="step", sample=True, max_points=50), df)
    assert len(_props(chart)["data"]) <= 50


@pytest.mark.parametrize(
    ("max_points", "says"),
    [
        pytest.param(50, True, id="read-far-more-than-drawn"),
        pytest.param(1000, False, id="read-about-what-it-draws"),
    ],
)
def test_a_line_chart_that_read_far_more_points_than_it_draws_says_how_many(
    max_points: int, *, says: bool
) -> None:
    """It is slow for what it shows, and only the logging can make it cheaper (`docs/client.md`)."""
    chart = LineChart.render(
        LineChartSettings(column="loss", x_axis="step", max_points=max_points), _metrics_df(2000)
    )

    notes = [str(props(node)["children"]) for node in chart._traverse() if isinstance(node, dmc.Text)]
    assert any(note.startswith("4,000 points read, ") for note in notes) is says


def test_line_chart_render_no_sample_keeps_all_points() -> None:
    df = _metrics_df(2000)
    chart = LineChart.render(LineChartSettings(column="loss", x_axis="step", sample=False), df)
    assert len(_props(chart)["data"]) == 2000


def test_line_chart_render_series_and_datakey() -> None:
    df = _metrics_df(20, n_runs=3)
    chart = LineChart.render(LineChartSettings(column="loss", x_axis="step"), df)
    props = _props(chart)
    assert props["dataKey"] == "step"
    assert {s["name"] for s in props["series"]} == {"1", "2", "3"}


def test_line_chart_series_label_defaults_to_run_id_without_a_run_name_column() -> None:
    df = _metrics_df(5, n_runs=2)
    chart = LineChart.render(LineChartSettings(column="loss", x_axis="step"), df)
    labels = {s["name"]: s["label"] for s in _props(chart)["series"]}
    assert labels == {"1": "Run 1", "2": "Run 2"}


def test_line_chart_series_label_uses_the_run_name_column_when_present() -> None:
    """`fetch_panel_dataframe` merges a `run_name` column onto the dataframe for charts that want
    to label by name instead of bare id -- `line_chart.py` must read it, not just carry it along."""
    df = _metrics_df(5, n_runs=2)
    df[RUN_NAME_COLUMN] = df["run_id"].map({1: "uptight-yak", 2: "abstract-goldfish"})
    chart = LineChart.render(LineChartSettings(column="loss", x_axis="step"), df)
    labels = {s["name"]: s["label"] for s in _props(chart)["series"]}
    assert labels == {"1": "uptight-yak", "2": "abstract-goldfish"}


@given(st.dictionaries(st.integers(1, 4), st.sets(st.integers(0, 20), min_size=1), min_size=1))
def test_line_chart_never_shows_a_run_where_it_did_not_log(logged: dict[int, set[int]]) -> None:
    """
    A run's line goes only through the steps it logged (not a staircase of nearest values), and stops at its last.

    Its tooltip, at any step of its own range, names its nearest logged value (the earlier on a tie) and none past it.
    Each logged value is `10 * step + run`, so a value says where it came from.
    """
    df = pd.DataFrame(
        [
            {"run_id": run, "step": step, "loss": 10.0 * step + run}
            for run, steps in logged.items()
            for step in steps
        ]
    )
    chart = LineChart.render(LineChartSettings(column="loss", x_axis="step", sample=False), df)

    rows: list[dict[str, float]] = _props(chart)["data"]
    assert [row["step"] for row in rows] == sorted(set().union(*logged.values()))
    for row, (run, steps) in ((row, run_steps) for row in rows for run_steps in logged.items()):
        step = row["step"]
        nearest = min(steps, key=lambda at: (abs(at - step), at))
        line, tooltip, tooltip_step = row[str(run)], row[f"{run}__y"], row[f"{run}__x"]
        assert line == 10 * step + run if step in steps else np.isnan(line)
        if min(steps) <= step <= max(steps):
            assert (tooltip, tooltip_step) == (10 * nearest + run, nearest)
        else:
            assert np.isnan([tooltip, tooltip_step]).all()


def test_line_chart_syncs_by_value_not_index() -> None:
    """Charts with the same syncId but different sampling rates must sync by x-value.

    Recharts' default syncMethod ("index") lines up points by row position, so two
    synced charts with different row counts (e.g. one sampled, one not) show the
    tooltip/crosshair at mismatched x-values. syncMethod="value" fixes that.
    """
    df = _metrics_df(20)
    chart = LineChart.render(LineChartSettings(column="loss", x_axis="step"), df)
    props = _props(chart)
    assert props["lineChartProps"]["syncMethod"] == "value"
    assert props["lineChartProps"]["syncId"] == "step"


@pytest.mark.parametrize(
    ("lrs", "dots"),
    [
        pytest.param({1: [0.1] * 3, 2: [0.1] * 3, 3: [0.01] * 3}, True, id="every-run-keeps-one-lr"),
        pytest.param(
            {1: [0.1, 0.05, 0.01], 2: [0.1, 0.05, 0.01]}, False, id="runs-sharing-an-lr-at-every-step"
        ),
        pytest.param({1: [0.1, 0.05, 0.01], 2: [0.1] * 3}, True, id="one-run-keeps-its-lr"),
    ],
)
def test_line_chart_over_a_shared_x_value_keeps_every_run_and_draws_a_run_of_one_point_as_a_dot(
    lrs: dict[int, list[float]], dots: bool
) -> None:
    """
    Runs with the same x (a learning rate) at the same step each have their own value there, and a run whose x never
    changes is one point, which has no line, so is drawn as a dot.
    """
    df = pd.DataFrame(
        [
            {"run_id": run_id, "step": step, "lr": lr, "grad_norm": 1.0 / (step + 1) + run_id}
            for run_id, run_lrs in lrs.items()
            for step, lr in enumerate(run_lrs)
        ]
    )
    chart = LineChart.render(LineChartSettings(column="grad_norm", x_axis="lr"), df)
    plot = _props(chart)
    assert [series["name"] for series in plot["series"]] == [str(run_id) for run_id in lrs]
    for run_id, run_lrs in lrs.items():
        mine = [row[str(run_id)] for row in plot["data"] if row[str(run_id)] == row[str(run_id)]]  # not NaN
        assert len(mine) == len(
            set(run_lrs)
        )  # a value for each of the x's it logged, whoever else logged them
    assert plot["withDots"] is dots


def test_line_chart_x_axis_defaults_to_numeric_scale() -> None:
    """Regression: Recharts' default XAxis type is "category" (equal pixel spacing per distinct
    x value, regardless of its numeric size), which makes irregularly-spaced steps look like
    they're nonlinearly compressing. A numeric x column like step/epoch should be spaced by its
    actual value by default.
    """
    df = _metrics_df(20)
    chart = LineChart.render(LineChartSettings(column="loss", x_axis="step"), df)
    assert _props(chart)["xAxisProps"] == {"type": "number"}


def test_line_chart_x_axis_type_is_configurable() -> None:
    df = _metrics_df(20)
    chart = LineChart.render(LineChartSettings(column="loss", x_axis="step", x_axis_type="category"), df)
    assert _props(chart)["xAxisProps"] == {"type": "category"}


@pytest.mark.parametrize(
    "settings",
    [
        LineChartSettings(column="loss", x_axis="timestamp_utc", x_axis_type="date", sample=False),
        BarChartSettings(column="loss", x_axis="timestamp_utc", x_axis_type="date"),
    ],
    ids=["line", "bar"],
)
def test_a_date_axis_over_values_that_are_no_dates_says_so_rather_than_drawing_nothing(
    settings: LineChartSettings | BarChartSettings,
) -> None:
    """Regression (#672): a line chart was drawn blank, and a bar chart drew the text as groups, with no word of why."""
    df = _metrics_df(3).assign(timestamp_utc=["soon", "later", "never"] * 2)
    match settings:
        case LineChartSettings():
            chart = LineChart.render(settings, df)
        case BarChartSettings():
            chart = BarChart.render(settings, df)

    assert "the axis can place" in str(props(chart))


def test_a_bar_chart_on_a_date_axis_groups_only_the_values_that_are_dates_in_date_order() -> None:
    """Regression (#672): one value that was no date left every value as text, sorted as text among the dates."""
    df = _metrics_df(3).assign(when=["2026-01-10T00:00:00Z", "soon", "2026-01-09T00:00:00Z"] * 2)

    chart = BarChart.render(
        BarChartSettings(column="loss", x_axis="when", x_axis_type="date", aggregation="mean"), df
    )

    assert list(_bars(chart, "when")) == [
        pd.Timestamp("2026-01-09", tz="UTC"),
        pd.Timestamp("2026-01-10", tz="UTC"),
    ]


def test_line_chart_x_axis_type_date_converts_timestamps_to_epoch_millis() -> None:
    """
    Regression: a timestamp x-axis (e.g. timestamp_utc) previously only offered "number"/
    "category" -- neither sensible for a real calendar timestamp. Recharts has no native time
    scale, so "date" renders as a numeric axis (epoch ms) with a date-formatting tick/tooltip.
    The domain must be the data's own range: Recharts' default `[0, "auto"]` would drag an epoch-ms
    axis back to 1970 and squeeze every real point into a sliver at the right edge.
    """
    df = _metrics_df(3).assign(
        timestamp_utc=["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z"] * 2
    )
    chart = LineChart.render(
        LineChartSettings(column="loss", x_axis="timestamp_utc", x_axis_type="date", sample=False), df
    )
    props = _props(chart)

    assert props["xAxisProps"] == {
        "type": "number",
        "tickFormatter": {"function": "lineChartDateTick"},
        "domain": ["dataMin", "dataMax"],
    }
    assert props["tooltipProps"]["labelFormatter"] == {"function": "lineChartTooltipLabelDate"}
    epoch_start = pd.Timestamp("2026-01-01T00:00:00Z").value // 10**6
    assert sorted(row["timestamp_utc"] for row in props["data"]) == [
        epoch_start,
        epoch_start + 1000,
        epoch_start + 2000,
    ]


def test_line_chart_js_function_names_are_defined_in_the_tooltip_js() -> None:
    """
    Every `{"function": "..."}` name `render` can hand to `xAxisProps`/`tooltipProps` must actually
    be registered on `window.dashMantineFunctions` in `line_chart_tooltip.js`, or the browser fails
    at render time with "No match for [name] in window.dashMantineFunctions" -- a typo/rename on
    either side breaks this silently at the Python level (nothing here calls the JS), so check the
    two files agree directly instead.
    """
    js_source = Path(line_chart.__file__).with_name("line_chart_tooltip.js").read_text()
    referenced_names = {
        *_TICK_FORMATTER.values(),
        *_TOOLTIP_LABEL_FORMATTER.values(),
        "lineChartTooltipLabel",
        _TOOLTIP_CONTENT,
    }
    for name in referenced_names:
        assert f"window.dashMantineFunctions.{name} =" in js_source, f"{name} is not defined in the JS"


def test_line_chart_x_axis_type_time_formats_as_a_duration() -> None:
    """A "time" x-axis (elapsed seconds, e.g. a run's training time) keeps its raw numeric values
    but formats ticks/tooltip as h:mm:ss instead of a bare number.
    """
    df = _metrics_df(3).assign(elapsed_seconds=[0, 65, 130] * 2)
    chart = LineChart.render(
        LineChartSettings(column="loss", x_axis="elapsed_seconds", x_axis_type="time", sample=False), df
    )
    props = _props(chart)

    assert props["xAxisProps"] == {
        "type": "number",
        "tickFormatter": {"function": "lineChartTimeTick"},
        "domain": ["dataMin", "dataMax"],
    }
    assert props["tooltipProps"]["labelFormatter"] == {"function": "lineChartTooltipLabelTime"}
    assert sorted(row["elapsed_seconds"] for row in props["data"]) == [0, 65, 130]


def test_line_chart_render_does_not_mutate_input_dataframe() -> None:
    """
    Regression: `render` used to coerce object/str columns to datetime (and, for a "date" x-axis,
    all the way to epoch-ms floats) *in place* on the caller's dataframe. `render_panel_charts`
    renders every chart in a panel against the very same fetched dataframe object -- one chart
    mutating it corrupted the input every later chart in that panel saw, however unrelated its own
    x-axis/columns were.
    """
    df = _metrics_df(3).assign(
        timestamp_utc=["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z"] * 2
    )
    before = df.copy()

    LineChart.render(LineChartSettings(column="loss", x_axis="timestamp_utc", x_axis_type="date"), df)

    pd.testing.assert_frame_equal(df, before)


def test_line_chart_date_x_axis_does_not_corrupt_a_later_chart_sharing_the_dataframe() -> None:
    """A "date" x-axis chart rendered first must not change what a second, unrelated chart in the
    same panel (sharing the same fetched dataframe) renders.
    """
    df = _metrics_df(3, n_runs=3).assign(
        timestamp_utc=["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z"] * 3,
        epoch=[0, 0, 1] * 3,
    )

    expected = LineChart.render(LineChartSettings(column="epoch", x_axis="step"), df.copy())
    LineChart.render(LineChartSettings(column="loss", x_axis="timestamp_utc", x_axis_type="date"), df)
    actual = LineChart.render(LineChartSettings(column="epoch", x_axis="step"), df)

    assert _props(actual)["data"] == _props(expected)["data"]
    assert _props(actual)["series"] == _props(expected)["series"]


def test_line_chart_sampled_siblings_share_x_values_for_syncing() -> None:
    """Two charts in the same panel (same shared dataframe), plotting different metrics, must
    sample the same x-values -- otherwise a synced tooltip only lines up where their independently
    picked sample sets happen to intersect.
    """
    n = 2000
    df = _metrics_df(n).assign(acc=lambda d: (d["loss"] * -1 + 3).round(2))

    loss_chart = LineChart.render(LineChartSettings(column="loss", x_axis="step", max_points=50), df)
    acc_chart = LineChart.render(LineChartSettings(column="acc", x_axis="step", max_points=50), df)

    loss_steps = {row["step"] for row in _props(loss_chart)["data"]}
    acc_steps = {row["step"] for row in _props(acc_chart)["data"]}
    assert loss_steps == acc_steps


def test_line_chart_averages_a_sampled_x_axis_that_repeats_within_a_run() -> None:
    """Several steps per epoch plot as one point per epoch, at the mean of the steps in it."""
    steps = 2000
    df = _metrics_df(steps, n_runs=1).assign(epoch=lambda d: d["step"] // 10, loss=lambda d: d["step"] % 10)

    chart = LineChart.render(LineChartSettings(column="loss", x_axis="epoch", max_points=20), df)

    data = _props(chart)["data"]
    assert 3 <= len(data) <= 20
    assert {row["1"] for row in data} == {4.5}


def _hparam_grouped_df(hidden_sizes: dict[int, int], accuracies: dict[int, list[float]]) -> pd.DataFrame:
    """One run per key in `hidden_sizes`; each run logs `accuracies[run_id]` at successive steps,
    plus a constant `hparam__hidden_size` hyperparameter column (as merged onto the wide metrics
    dataframe by `merge_hyperparams`)."""
    frames = [
        pd.DataFrame(
            {
                "run_id": run_id,
                "step": range(len(values)),
                "accuracy": values,
                "hparam__hidden_size": hidden_sizes[run_id],
            }
        )
        for run_id, values in accuracies.items()
    ]
    return pd.concat(frames, ignore_index=True)


def _bars(chart: dmc.Stack, x_axis: str) -> dict[object, dict[str, float]]:
    """A bar chart's groups, each as the runs' bars in it (by run id): what was drawn, less what labels it."""
    rows: list[dict[str, Any]] = _props(chart)["data"]
    return {
        row[x_axis]: {key: value for key, value in row.items() if key not in (x_axis, "__x_axis_name__")}
        for row in rows
    }


def test_bar_chart_draws_a_bar_for_each_run_in_the_group_it_shares() -> None:
    """Runs with one hidden_size are bars side by side in its group, each in its own colour (#493)."""
    df = _hparam_grouped_df({1: 128, 2: 128, 3: 256}, {1: [0.8], 2: [0.9], 3: [0.7]})
    chart = BarChart.render(BarChartSettings(column="accuracy", x_axis="hidden_size"), df)
    props = _props(chart)
    assert _bars(chart, "hidden_size") == {128: {"1": 0.8, "2": 0.9}, 256: {"3": 0.7}}
    assert [(s["name"], s["color"]) for s in props["series"]] == [
        ("1", series_color(1)),
        ("2", series_color(2)),
        ("3", series_color(3)),
    ]
    assert props["type"] == "default"


def test_bar_chart_fills_each_group_with_its_bar_when_no_group_has_more_than_one() -> None:
    """Side by side, a lone bar would sit in a slot of its own, among the slots of runs that are not in its group."""
    df = _hparam_grouped_df({1: 128, 2: 256}, {1: [0.8], 2: [0.9]})
    chart = BarChart.render(BarChartSettings(column="accuracy", x_axis="hidden_size"), df)
    assert _props(chart)["type"] == "stacked"


@pytest.mark.parametrize(
    ("names", "x_axis_type", "labels"),
    [
        pytest.param(
            {1: "uptight-yak", 2: "abstract-goldfish"},
            "auto",
            ["uptight-yak", "abstract-goldfish"],
            id="named",
        ),
        pytest.param(
            {1: "uptight-yak", 2: None}, "auto", ["uptight-yak", "Run 2"], id="a-run-without-a-name"
        ),
        pytest.param({1: "same", 2: "same"}, "auto", ["same (1)", "same (2)"], id="two-runs-with-one-name"),
        pytest.param(
            {1: "uptight-yak", 2: "abstract-goldfish"},
            "category",
            ["uptight-yak", "abstract-goldfish"],
            id="ids-as-text",
        ),
    ],
)
def test_bar_chart_of_runs_labels_each_bar_with_its_run_name(
    names: dict[int, str | None], x_axis_type: Literal["auto", "category"], labels: list[str]
) -> None:
    """A bar per run (the x-axis being the run) is told apart by name -- a bare run id says little about it."""
    df = pd.DataFrame(
        {
            "run_id": [1, 1, 2],
            "step": [0, 1, 0],
            "accuracy": [0.1, 0.8, 0.6],
            RUN_NAME_COLUMN: [names[1], names[1], names[2]],
        }
    )
    chart = BarChart.render(BarChartSettings(column="accuracy", x_axis="run_id", x_axis_type=x_axis_type), df)
    props = _props(chart)
    assert [row["run_id"] for row in props["data"]] == labels
    assert [row[run] for row, run in zip(props["data"], ["1", "2"], strict=True)] == [
        pytest.approx(0.8),
        pytest.approx(0.6),
    ]
    assert {row["__x_axis_name__"] for row in props["data"]} == {"run"}  # what the tooltip prefixes it with
    assert props["xAxisLabel"] == "run"
    assert props["type"] == "stacked"  # each run's bar fills its own group
    assert props["withLegend"] is False  # the groups say which run each bar is of


def test_bar_chart_of_runs_without_names_keeps_the_run_ids() -> None:
    df = pd.DataFrame({"run_id": [1, 2], "step": [0, 0], "accuracy": [0.1, 0.6]})
    chart = BarChart.render(BarChartSettings(column="accuracy", x_axis="run_id"), df)
    assert [row["run_id"] for row in _props(chart)["data"]] == [1, 2]


def test_bar_chart_render_does_not_mutate_input_dataframe() -> None:
    """
    Same hazard as `test_line_chart_render_does_not_mutate_input_dataframe`: a panel with both a
    bar chart and a line chart renders every chart against the same fetched dataframe object. Uses
    a string x-axis column specifically -- `render`'s object/str -> datetime coercion (the part
    that used to mutate in place) only ever touches `x_axis`/`column`, so this is the case that
    actually exercises it (a plain numeric `hidden_size` x-axis wouldn't).
    """
    df = _hparam_grouped_df({1: 128, 2: 128, 3: 256}, {1: [0.8], 2: [0.9], 3: [0.7]}).assign(
        logged_at=["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z"]
    )
    before = df.copy()

    BarChart.render(BarChartSettings(column="accuracy", x_axis="logged_at"), df)

    pd.testing.assert_frame_equal(df, before)


def test_bar_chart_draws_the_last_logged_value_of_a_run_unless_told_to_aggregate() -> None:
    """A run that logs the metric at every step draws its final value, not every step: and so reads no more."""
    df = _hparam_grouped_df({1: 128, 2: 128}, {1: [0.1, 0.2, 0.9], 2: [0.5]})
    settings = BarChartSettings(column="accuracy", x_axis="hidden_size")
    assert _bars(BarChart.render(settings, df), "hidden_size") == {128: {"1": 0.9, "2": 0.5}}
    assert BarChart.data_request(settings).reductions.default is MetricReduction.LATEST


@pytest.mark.parametrize(
    ("aggregation", "expected"),
    [("mean", 0.4), ("median", 0.2), ("min", 0.1), ("max", 0.9), ("sum", 1.2), ("count", 3)],
)
def test_bar_chart_aggregates_what_a_run_logged_in_a_group(
    aggregation: Literal["mean", "median", "min", "max", "sum", "count"], expected: float
) -> None:
    df = _hparam_grouped_df({1: 128, 2: 128}, {1: [0.1, 0.2, 0.9], 2: [0.5]})
    settings = BarChartSettings(column="accuracy", x_axis="hidden_size", aggregation=aggregation)
    bars = _bars(BarChart.render(settings, df), "hidden_size")[128]
    assert bars["1"] == pytest.approx(expected)
    assert BarChart.data_request(settings).reductions.default is MetricReduction.HISTORY


def test_bar_chart_groups_the_steps_a_run_logged_by_an_x_axis_of_a_metric() -> None:
    """Each epoch is a group, and in it each run's bar is the aggregate of the steps it logged in that epoch (#493)."""
    df = pd.DataFrame(
        {
            "run_id": [1] * 4 + [2] * 4,
            "step": [0, 1, 2, 3] * 2,
            "epoch": [0, 0, 1, 1] * 2,
            "loss": [4.0, 2.0, 1.0, 0.5, 8.0, 4.0, 2.0, 1.0],
        }
    )
    chart = BarChart.render(BarChartSettings(column="loss", x_axis="epoch", aggregation="mean"), df)
    assert _bars(chart, "epoch") == {0: {"1": 3.0, "2": 6.0}, 1: {"1": 0.75, "2": 1.5}}


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        pytest.param("ascending", [32, 128, 256], id="ascending-numerically"),
        pytest.param("none", [256, 32, 128], id="first-seen-order"),
        pytest.param("descending", [256, 128, 32], id="descending"),
    ],
)
def test_bar_chart_orders_its_groups_as_its_sort_setting_says(
    sort: Literal["none", "ascending", "descending"], expected: list[int]
) -> None:
    df = _hparam_grouped_df({1: 256, 2: 32, 3: 128}, {1: [0.1], 2: [0.2], 3: [0.3]})
    chart = BarChart.render(BarChartSettings(column="accuracy", x_axis="hidden_size", sort=sort), df)
    assert [row["hidden_size"] for row in _props(chart)["data"]] == expected


def test_bar_chart_x_axis_type_category_forces_text_sort_over_numeric() -> None:
    """A numeric-looking category (e.g. version strings) can be forced to sort as plain text."""
    df = pd.DataFrame(
        {"run_id": [1, 2, 3], "step": [0, 0, 0], "version": ["v2", "v10", "v1"], "accuracy": [0.1, 0.2, 0.3]}
    )
    chart = BarChart.render(BarChartSettings(column="accuracy", x_axis="version", x_axis_type="category"), df)
    assert list(_bars(chart, "version")) == ["v1", "v10", "v2"]


def test_bar_chart_over_a_metric_draws_each_run_at_its_own_latest_value() -> None:
    """x_axis falls back to a plain metric column (its own last-logged step per run) when no
    hparam column matches it -- e.g. comparing final loss across runs by their final step."""
    df = pd.concat(
        [
            pd.DataFrame({"run_id": 1, "step": [0, 1, 2], "loss": [0.9, 0.5, 0.1]}),
            pd.DataFrame({"run_id": 2, "step": [0, 1], "loss": [0.9, 0.3]}),
        ],
        ignore_index=True,
    )
    chart = BarChart.render(BarChartSettings(column="loss", x_axis="step"), df)
    assert _props(chart)["dataKey"] == "step"
    assert _bars(chart, "step") == {1: {"2": 0.3}, 2: {"1": 0.1}}


def test_bar_chart_over_a_metric_keeps_runs_that_end_on_one_value_apart() -> None:
    """A learning rate that decays to the same value in every run drew a single bar, saying nothing of the runs (#422)."""
    df = pd.DataFrame(
        {
            "run_id": [1, 1, 2, 2],
            "step": [0, 1, 0, 1],
            "lr": [0.1, 0.001, 0.2, 0.001],
            "grad_norm": [1.0, 0.5, 2.0, 0.7],
            RUN_NAME_COLUMN: ["one", "one", "two", "two"],
        }
    )
    chart = BarChart.render(BarChartSettings(column="grad_norm", x_axis="lr"), df)

    assert _bars(chart, "lr") == {0.001: {"1": 0.5, "2": 0.7}}
    assert [(s["name"], s["label"]) for s in _props(chart)["series"]] == [("1", "one"), ("2", "two")]


def test_bar_chart_series_and_labels() -> None:
    df = _hparam_grouped_df({1: 128}, {1: [0.8]})
    chart = BarChart.render(BarChartSettings(column="accuracy", x_axis="hidden_size"), df)
    props = _props(chart)
    assert props["series"] == [{"name": "1", "label": "Run 1", "color": series_color(1)}]
    assert "yAxisLabel" not in props


def test_bar_chart_x_axis_is_categorical() -> None:
    """Bars are inherently discrete, unlike a line chart's optionally-numeric x-axis."""
    df = _hparam_grouped_df({1: 128}, {1: [0.8]})
    chart = BarChart.render(BarChartSettings(column="accuracy", x_axis="hidden_size"), df)
    assert _props(chart)["xAxisProps"] == {"type": "category"}


def test_bar_chart_vertical_orientation_flips_category_axis_and_labels() -> None:
    df = _hparam_grouped_df({1: 128}, {1: [0.8]})
    chart = BarChart.render(
        BarChartSettings(column="accuracy", x_axis="hidden_size", orientation="vertical"), df
    )
    props = _props(chart)
    assert props["orientation"] == "vertical"
    assert props["xAxisProps"] == {}
    assert props["yAxisProps"] == {"type": "category"}
    assert props["xAxisLabel"] == "latest(accuracy)"
    assert "yAxisLabel" not in props


def test_bar_chart_syncs_by_value_not_index() -> None:
    df = _hparam_grouped_df({1: 128}, {1: [0.8]})
    chart = BarChart.render(BarChartSettings(column="accuracy", x_axis="hidden_size"), df)
    props = _props(chart)
    assert props["barChartProps"]["syncMethod"] == "value"

    assert props["barChartProps"]["syncId"] == "hidden_size"


def test_line_chart_natural_width_derives_from_height() -> None:
    settings = LineChartSettings(column="loss", x_axis="step", height=180)
    assert LineChart.natural_width(settings) == round(180 * 16 / 9)


def test_line_chart_natural_width_override() -> None:
    settings = LineChartSettings(column="loss", x_axis="step", height=180, width=500)
    assert LineChart.natural_width(settings) == 500


def test_bar_chart_natural_width_derives_from_height() -> None:
    settings = BarChartSettings(column="loss", x_axis="step", height=180)
    assert BarChart.natural_width(settings) == round(180 * 16 / 9)


def test_bar_chart_natural_width_override() -> None:
    settings = BarChartSettings(column="loss", x_axis="step", height=180, width=500)
    assert BarChart.natural_width(settings) == 500


LONG_METRIC = "DeviceStatsMonitor.on_test_batch_end/active.all.allocated"


@pytest.mark.parametrize("limit", [MIN_MAX_AXIS_LABEL_CHARS, 20, DEFAULT_MAX_AXIS_LABEL_CHARS])
def test_line_chart_shortens_a_long_x_axis_name_to_the_chosen_axis_title_length(limit: int) -> None:
    df = _metrics_df(20, n_runs=1)
    df[LONG_METRIC] = df["step"]

    chart = LineChart.render(
        LineChartSettings(column="loss", x_axis=LONG_METRIC, max_axis_label_chars=limit), df
    )

    label = _props(chart)["xAxisLabel"]
    assert label.startswith("…")
    assert len(label) == limit
    assert LONG_METRIC.endswith(label.removeprefix("…"))  # pyrefly: ignore [unknown-argument-type]


@pytest.mark.parametrize("limit", [MIN_MAX_AXIS_LABEL_CHARS, 20, DEFAULT_MAX_AXIS_LABEL_CHARS])
def test_bar_chart_shortens_a_long_metric_name_inside_its_aggregation_label(limit: int) -> None:
    df = _hparam_grouped_df({1: 128}, {1: [0.8]}).rename(columns={"accuracy": LONG_METRIC})

    chart = BarChart.render(
        BarChartSettings(
            column=LONG_METRIC, x_axis="hidden_size", orientation="vertical", max_axis_label_chars=limit
        ),
        df,
    )

    label = _props(chart)["xAxisLabel"]
    assert label.startswith("latest(…")
    assert label.endswith(")")
    assert len(label) == limit


@pytest.mark.parametrize("settings", [LineChartSettings, BarChartSettings])
def test_an_axis_title_limit_too_small_to_keep_a_name_recognizable_is_rejected(
    settings: type[LineChartSettings | BarChartSettings],
) -> None:
    with pytest.raises(ValidationError):
        settings(column="loss", x_axis="step", max_axis_label_chars=MIN_MAX_AXIS_LABEL_CHARS - 1)


@pytest.mark.parametrize("chart_type", [LineChart, BarChart])
def test_a_chart_of_its_own_x_axis_is_refused_so_a_page_drawing_by_epoch_leaves_its_epoch_chart_alone(
    chart_type: type[LineChart | BarChart],
) -> None:
    """Regression: a page's x axis of `epoch` drew the chart of `epoch` against itself, which failed to render."""
    chart_type.register(allow_override=True)
    with pytest.raises(ValidationError, match="against itself"):
        chart_type.parameter_type()(column="epoch", x_axis="epoch")
    page = [{"x_axis": "epoch"}]
    epoch, loss = (
        ChartInstance[Any, Any](chart_type=chart_type.name, parameters={"column": column, "x_axis": "step"})
        for column in ("epoch", "loss")
    )

    assert epoch.drawn_with(page).parameters["x_axis"] == "step"
    assert loss.drawn_with(page).parameters["x_axis"] == "epoch"


def test_line_chart_gives_its_value_axis_room_for_ticks_and_no_title_of_its_own() -> None:
    chart = LineChart.render(LineChartSettings(column="loss", x_axis="step"), _metrics_df(5))

    assert "yAxisLabel" not in _props(chart)
    y_axis = _props(chart)["yAxisProps"]
    assert y_axis["width"] == VALUE_AXIS_WIDTH
    js_source = Path(line_chart.__file__).with_name("line_chart_tooltip.js").read_text()
    assert f"window.dashMantineFunctions.{y_axis['tickFormatter']['function']} =" in js_source


def test_line_chart_tooltip_is_titled_with_the_whole_metric_name() -> None:
    df = _metrics_df(20, n_runs=1).rename(columns={"loss": LONG_METRIC})

    chart = LineChart.render(LineChartSettings(column=LONG_METRIC, x_axis="step"), df)

    assert _props(chart)["tooltipProps"]["chartTitle"] == LONG_METRIC


@pytest.mark.parametrize(("orientation", "gets_room_for_ticks"), [("horizontal", True), ("vertical", False)])
def test_bar_chart_gives_its_value_axis_room_for_ticks_only_when_the_values_are_on_it(
    orientation: Literal["horizontal", "vertical"], gets_room_for_ticks: bool
) -> None:
    df = _hparam_grouped_df({1: 128}, {1: [0.8]})

    chart = BarChart.render(
        BarChartSettings(column="accuracy", x_axis="hidden_size", orientation=orientation), df
    )

    y_axis = _props(chart)["yAxisProps"]
    assert (y_axis.get("width") == VALUE_AXIS_WIDTH) is gets_room_for_ticks
    if gets_room_for_ticks:
        js_source = Path(line_chart.__file__).with_name("bar_chart_tooltip.js").read_text()
        assert f"window.dashMantineFunctions.{y_axis['tickFormatter']['function']} =" in js_source


def _line(df: pd.DataFrame) -> dmc.Stack:
    return LineChart.render(LineChartSettings(column=LONG_METRIC, x_axis="step"), df)


def _bar(df: pd.DataFrame) -> dmc.Stack:
    return BarChart.render(BarChartSettings(column=LONG_METRIC, x_axis="step"), df)


@pytest.mark.parametrize(
    ("render", "expected"),
    [(_line, LONG_METRIC), (_bar, f"latest({LONG_METRIC})")],
    ids=["line", "bar"],
)
def test_a_metric_chart_is_titled_with_its_full_metric_name(
    render: Callable[[pd.DataFrame], dmc.Stack], expected: str
) -> None:
    """The value axis has no title of its own: the heading above the plot names the whole metric (and a bar's aggregation)."""
    df = _metrics_df(3, n_runs=1).rename(columns={"loss": LONG_METRIC})

    title, _ = props(render(df))["children"]

    assert props(title)["children"] == expected
