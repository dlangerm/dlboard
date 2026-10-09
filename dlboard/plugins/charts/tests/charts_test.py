# pyright: reportPrivateUsage=false
"""Tests for LTTB downsampling and the line chart that relies on it."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import pytest

from dlboard.conftest import props as _props
from dlboard.models import RUN_NAME_COLUMN
from dlboard.plugins.charts import line_chart
from dlboard.plugins.charts._axis_label import MAX_AXIS_LABEL_CHARS
from dlboard.plugins.charts._sampling import downsample_grouped, downsample_series, shared_sample_grid
from dlboard.plugins.charts.bar_chart import BarChart, BarChartSettings
from dlboard.plugins.charts.line_chart import (
    _TICK_FORMATTER,
    _TOOLTIP_CONTENT,
    _TOOLTIP_LABEL_FORMATTER,
    LineChart,
    LineChartSettings,
)


def _series_df(n: int) -> pd.DataFrame:
    return pd.DataFrame({"x": range(n), "y": np.sin(np.linspace(0, 10, n))})


@pytest.mark.parametrize("n", [10, 50])
def test_downsample_series_is_noop_below_threshold(n: int) -> None:
    df = _series_df(n)
    assert len(downsample_series(df, "x", "y", max_points=100)) == n


@pytest.mark.parametrize("max_points", [10, 50, 100])
def test_downsample_series_bounds_and_preserves_endpoints(max_points: int) -> None:
    df = _series_df(1000)
    out = downsample_series(df, "x", "y", max_points=max_points)
    assert len(out) == max_points
    assert out["x"].iloc[0] == df["x"].iloc[0]
    assert out["x"].iloc[-1] == df["x"].iloc[-1]
    assert out["x"].is_monotonic_increasing


def test_downsample_grouped_downsamples_each_group_independently() -> None:
    df = pd.concat([_series_df(1000).assign(run_id=1), _series_df(50).assign(run_id=2)], ignore_index=True)
    out = downsample_grouped(df, "x", "y", "run_id", max_points=100)
    counts = out.groupby("run_id").size()
    assert counts[1] == 100
    assert counts[2] == 50  # below threshold, untouched


def test_downsample_grouped_empty_df_is_noop() -> None:
    df = pd.DataFrame(columns=["x", "y", "run_id"])
    assert downsample_grouped(df, "x", "y", "run_id", max_points=10).empty


def test_shared_sample_grid_gives_every_value_column_the_same_x_values() -> None:
    """Two metrics with unrelated shapes must still pick the same x-values, so charts plotting
    either one -- synced by x-value -- always have a point at the same place.
    """
    n = 1000
    x = np.arange(n)
    df = pd.DataFrame(
        {
            "x": x,
            "run_id": 1,
            "loss": np.sin(np.linspace(0, 10, n)),
            "acc": np.cos(np.linspace(0, 30, n)),  # different shape/frequency than "loss"
        }
    )
    grid = shared_sample_grid(df, x_col="x", group_col="run_id", value_cols=["loss", "acc"], max_points=50)

    loss_only = shared_sample_grid(df, x_col="x", group_col="run_id", value_cols=["loss"], max_points=50)
    acc_only = shared_sample_grid(df, x_col="x", group_col="run_id", value_cols=["acc"], max_points=50)
    assert set(loss_only["x"]) <= set(grid["x"])
    assert set(acc_only["x"]) <= set(grid["x"])


def test_shared_sample_grid_empty_df_is_noop() -> None:
    df = pd.DataFrame(columns=["x", "y", "run_id"])
    assert shared_sample_grid(df, "x", "run_id", ["y"], max_points=10).empty


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


@pytest.mark.parametrize(
    ("column", "has_value_at"),
    [("2", {0, 4, 8}), ("2__y", set(range(9)))],
    ids=["plotted-line-only-where-logged", "tooltip-nearest-value-within-the-runs-range"],
)
def test_line_chart_never_shows_a_run_where_it_did_not_log(column: str, has_value_at: set[int]) -> None:
    """
    Run 2 logs steps 0, 4 and 8 of run 1's 0-11. Its line is drawn only through what it logged (not
    as a staircase of nearest values), and stops at step 8 rather than running flat to 11; the
    tooltip's nearest value covers its own range and nothing past it.
    """
    df = pd.concat(
        [_metrics_df(12, n_runs=1), _metrics_df(9, n_runs=2).query("run_id == 2 and step % 4 == 0")]
    )
    chart = LineChart.render(LineChartSettings(column="loss", x_axis="step", sample=False), df)

    values: dict[int, float] = {row["step"]: row[column] for row in _props(chart)["data"]}
    assert {step for step, value in values.items() if not np.isnan(value)} == has_value_at


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


def test_line_chart_x_axis_type_date_converts_timestamps_to_epoch_millis() -> None:
    """
    Regression: a timestamp x-axis (e.g. timestamp_utc) previously only offered "number"/
    "category" -- neither sensible for a real calendar timestamp. Recharts has no native time
    scale, so "date" renders as a numeric axis (epoch ms) with a date-formatting tick/tooltip.
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


def test_line_chart_x_axis_type_date_domain_does_not_default_to_the_unix_epoch() -> None:
    """
    Regression: Recharts' default numeric-axis domain is `[0, "auto"]`, not the data's own range.
    For an epoch-ms "date" axis, that drags the left edge back to 1970 regardless of how recent the
    real data is, squeezing every actual point into a sliver at the far right of the chart --
    visually "the zero on the x-axis is 1969" instead of the data's own earliest date.
    """
    df = _metrics_df(3).assign(
        timestamp_utc=["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z"] * 2
    )
    chart = LineChart.render(LineChartSettings(column="loss", x_axis="timestamp_utc", x_axis_type="date"), df)
    assert _props(chart)["xAxisProps"]["domain"] == ["dataMin", "dataMax"]


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


def test_bar_chart_aggregates_runs_sharing_an_x_value() -> None:
    """Multiple runs with the same hidden_size collapse into one bar, aggregated across runs."""
    df = _hparam_grouped_df({1: 128, 2: 128, 3: 256}, {1: [0.8], 2: [0.9], 3: [0.7]})
    chart = BarChart.render(BarChartSettings(column="accuracy", x_axis="hidden_size"), df)
    data = {row["hidden_size"]: row["accuracy"] for row in _props(chart)["data"]}
    assert data == {128: pytest.approx(0.85), 256: pytest.approx(0.7)}


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


def test_bar_chart_uses_last_logged_value_per_run() -> None:
    """A run that logs the metric at every step contributes only its final value, not every step."""
    df = _hparam_grouped_df({1: 128, 2: 128}, {1: [0.1, 0.2, 0.9], 2: [0.5]})
    chart = BarChart.render(BarChartSettings(column="accuracy", x_axis="hidden_size"), df)
    data = {row["hidden_size"]: row["accuracy"] for row in _props(chart)["data"]}
    assert data == {128: pytest.approx(0.7)}  # mean(0.9, 0.5), not mean(0.1, 0.2, 0.9, 0.5)


@pytest.mark.parametrize(
    ("aggregation", "expected"), [("mean", 0.75), ("min", 0.5), ("max", 1.0), ("sum", 1.5), ("count", 2)]
)
def test_bar_chart_aggregation_is_configurable(
    aggregation: Literal["mean", "median", "min", "max", "sum", "count"], expected: float
) -> None:
    df = _hparam_grouped_df({1: 128, 2: 128}, {1: [0.5], 2: [1.0]})
    chart = BarChart.render(
        BarChartSettings(column="accuracy", x_axis="hidden_size", aggregation=aggregation), df
    )
    data = {row["hidden_size"]: row["accuracy"] for row in _props(chart)["data"]}
    assert data[128] == pytest.approx(expected)


def test_bar_chart_sorts_numeric_groups_ascending() -> None:
    df = _hparam_grouped_df({1: 256, 2: 32, 3: 128}, {1: [0.1], 2: [0.2], 3: [0.3]})
    chart = BarChart.render(BarChartSettings(column="accuracy", x_axis="hidden_size"), df)
    assert [row["hidden_size"] for row in _props(chart)["data"]] == [32, 128, 256]


def test_bar_chart_sort_disabled_keeps_first_seen_order() -> None:
    df = _hparam_grouped_df({1: 256, 2: 32, 3: 128}, {1: [0.1], 2: [0.2], 3: [0.3]})
    chart = BarChart.render(BarChartSettings(column="accuracy", x_axis="hidden_size", sort="none"), df)
    assert [row["hidden_size"] for row in _props(chart)["data"]] == [256, 32, 128]


def test_bar_chart_sort_descending() -> None:
    df = _hparam_grouped_df({1: 256, 2: 32, 3: 128}, {1: [0.1], 2: [0.2], 3: [0.3]})
    chart = BarChart.render(BarChartSettings(column="accuracy", x_axis="hidden_size", sort="descending"), df)
    assert [row["hidden_size"] for row in _props(chart)["data"]] == [256, 128, 32]


def test_bar_chart_x_axis_type_category_forces_text_sort_over_numeric() -> None:
    """A numeric-looking category (e.g. version strings) can be forced to sort as plain text."""
    df = pd.DataFrame(
        {"run_id": [1, 2, 3], "step": [0, 0, 0], "version": ["v2", "v10", "v1"], "accuracy": [0.1, 0.2, 0.3]}
    )
    chart = BarChart.render(BarChartSettings(column="accuracy", x_axis="version", x_axis_type="category"), df)
    assert [row["version"] for row in _props(chart)["data"]] == ["v1", "v10", "v2"]


def test_bar_chart_groups_by_metric_column_when_no_hparam_matches() -> None:
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
    props = _props(chart)
    assert props["dataKey"] == "step"
    data = {row["step"]: row["loss"] for row in props["data"]}
    assert data == {1: pytest.approx(0.3), 2: pytest.approx(0.1)}


def test_bar_chart_series_and_labels() -> None:
    df = _hparam_grouped_df({1: 128}, {1: [0.8]})
    chart = BarChart.render(BarChartSettings(column="accuracy", x_axis="hidden_size"), df)
    props = _props(chart)
    assert props["series"] == [{"name": "accuracy", "label": "accuracy", "color": "var(--dl-series-1)"}]
    assert props["yAxisLabel"] == "mean(accuracy)"


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
    assert props["xAxisLabel"] == "mean(accuracy)"
    assert props["yAxisLabel"] == "hidden_size"


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


def test_line_chart_shortens_a_long_metric_name_to_fit_its_axis_title() -> None:
    df = _metrics_df(20, n_runs=1).rename(columns={"loss": LONG_METRIC})

    chart = LineChart.render(LineChartSettings(column=LONG_METRIC, x_axis="step"), df)

    label = _props(chart)["yAxisLabel"]
    assert label.startswith("…")
    assert len(label) <= MAX_AXIS_LABEL_CHARS
    assert LONG_METRIC.endswith(label.removeprefix("…"))


def test_bar_chart_shortens_a_long_metric_name_inside_its_aggregation_label() -> None:
    df = _hparam_grouped_df({1: 128}, {1: [0.8]}).rename(columns={"accuracy": LONG_METRIC})

    chart = BarChart.render(BarChartSettings(column=LONG_METRIC, x_axis="hidden_size"), df)

    label = _props(chart)["yAxisLabel"]
    assert label.startswith("mean(…")
    assert label.endswith(")")
    assert len(label) <= MAX_AXIS_LABEL_CHARS
