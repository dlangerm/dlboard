"""A line chart's runs grouped by a hyperparameter's value: a mean in a band per value, over the runs drawn (#699)."""

from __future__ import annotations

import zlib
from typing import Any

import pandas as pd
import pytest
from dash import dcc

from dlboard.models import RenderContext, hparam_column
from dlboard.plugins.charts._run_groups import grouped_traces, groups_of
from dlboard.plugins.charts.line_chart import LineChart, LineChartSettings
from dlboard.serve import series_color


def _seeds() -> pd.DataFrame:
    """Two seeds of lr=0.1 and one of lr=0.01 (and a run given no lr), logging loss at steps 0 and 1."""
    losses = {1: (4.0, 2.0), 2: (2.0, 1.0), 3: (8.0, 6.0), 4: (5.0, 5.0)}
    learning_rates: dict[int, float | None] = {1: 0.1, 2: 0.1, 3: 0.01, 4: None}
    frame = pd.DataFrame(
        [
            {"run_id": run, "step": step, "loss": loss}
            for run, pair in losses.items()
            for step, loss in enumerate(pair)
        ]
    )
    frame[hparam_column("lr")] = [learning_rates[run] for run in frame["run_id"]]
    return frame


def _traces(**settings: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    chart = LineChart.render_in(
        LineChartSettings(column="loss", x_axis="step", group_by="lr", sample=False, **settings),
        _seeds(),
        RenderContext(),
    )
    (store,) = [node for node in chart._traverse() if isinstance(node, dcc.Store)]
    return store.data["data"]  # pyrefly: ignore [missing-attribute]


def test_each_run_is_in_the_group_of_its_value_and_one_given_none_in_its_own() -> None:
    assert groups_of(_seeds(), "lr") == {1: "0.1", 2: "0.1", 3: "0.01", 4: "(none)"}


def test_a_group_is_its_runs_mean_in_a_band_from_the_lowest_to_the_highest() -> None:
    traces = _traces()

    means = {trace["name"]: trace["y"] for trace in traces if "name" in trace}
    assert means == {
        "lr=(none) (1 runs)": [5.0, 5.0],
        "lr=0.01 (1 runs)": [8.0, 6.0],
        "lr=0.1 (2 runs)": [3.0, 1.5],
    }
    high, low, _mean = traces[6:9]  # lr=0.1's: its band, then its mean
    assert (high["y"], low["y"], low["fill"]) == ([4.0, 2.0], [2.0, 1.0], "tonexty")


def test_a_groups_color_comes_from_its_value_on_every_chart() -> None:
    (mean,) = [trace for trace in _traces() if trace.get("name") == "lr=0.1 (2 runs)"]

    assert mean["line"]["color"] == series_color(zlib.crc32(b"0.1"))


def test_groups_past_max_groups_are_left_out_and_counted() -> None:
    pivot = pd.DataFrame({"step": [0], "1__y": [1.0], "2__y": [2.0], "3__y": [3.0]})

    traces, left_out = grouped_traces(pivot, "step", {1: "a", 2: "b", 3: "c"}, "opt", max_groups=2)

    assert ([trace["name"] for trace in traces if "name" in trace], left_out) == (
        ["opt=a (1 runs)", "opt=b (1 runs)"],
        1,
    )


@pytest.mark.parametrize(("group_by", "hparams"), [("lr", frozenset({"lr"})), ("", frozenset())])
def test_only_a_grouped_chart_reads_the_hyperparameter(group_by: str, hparams: frozenset[str]) -> None:
    request = LineChart.data_request(LineChartSettings(column="loss", x_axis="step", group_by=group_by))

    assert request.hparams == hparams
