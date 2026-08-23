# pyright: reportPrivateUsage=false
"""Tests for LTTB downsampling and the line chart that relies on it."""

from __future__ import annotations

from typing import Any, cast

import numpy as np
import pandas as pd
import pytest

from dltrack.plugins.charts._sampling import downsample_grouped, downsample_series
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


def _props(chart: object) -> dict[str, Any]:
    # dash-mantine-components ships no py.typed marker, so its component attrs are Unknown to pyright.
    return cast("Any", chart).to_plotly_json()["props"]


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
