# pyright: reportPrivateUsage=false
"""Tests for LTTB downsampling and the line chart that relies on it."""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
import pytest

from dltrack.conftest import props as _props
from dltrack.plugins.charts._sampling import downsample_grouped, downsample_series, shared_sample_grid
from dltrack.plugins.charts.bar_chart import BarChart, BarChartSettings
from dltrack.plugins.charts.line_chart import LineChart, LineChartSettings


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
    chart = BarChart.render(BarChartSettings(column="accuracy", x_axis="hidden_size", sort=False), df)
    assert [row["hidden_size"] for row in _props(chart)["data"]] == [256, 32, 128]


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
    assert props["series"] == [{"name": "accuracy", "label": "accuracy", "color": "blue.6"}]
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
