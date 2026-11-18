"""Scatter and parallel coordinates: a sweep's runs by hyperparameter, a row per run read, in the runs' colors (#698)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import dash_mantine_components as dmc
import pandas as pd
import pytest
from dash import dcc

from dlboard.models import EVERY_KEY, RUN_NAME_COLUMN, MetricReduction, hparam_column
from dlboard.plugins.charts.parallel_coordinates import ParallelCoordinatesChart, ParallelCoordinatesSettings
from dlboard.plugins.charts.scatter import ScatterChart, ScatterSettings
from dlboard.serve import series_color

if TYPE_CHECKING:
    from dash.development.base_component import Component


def _sweep() -> pd.DataFrame:
    """Three runs' losses over two steps, with their learning rate, optimizer and a seed they share."""
    rows = [
        {"run_id": run, "step": step, "loss": loss, RUN_NAME_COLUMN: f"run-{run}"}
        for run, losses in {1: (2.0, 1.0), 2: (2.0, 0.5), 3: (2.0, 0.8)}.items()
        for step, loss in enumerate(losses)
    ]
    frame = pd.DataFrame(rows)
    hparams: dict[str, dict[int, float | str]] = {
        "lr": {1: 0.1, 2: 0.01, 3: 0.001},
        "optimizer": {1: "adam", 2: "sgd", 3: "adam"},
        "seed": {1: 0, 2: 0, 3: 0},
    }
    for key, by_run in hparams.items():
        frame[hparam_column(key)] = [by_run[run] for run in frame["run_id"]]
    return frame


def _figure(chart: Component) -> dict[str, Any]:
    (store,) = [node for node in chart._traverse() if isinstance(node, dcc.Store)]
    return store.data  # pyrefly: ignore [missing-attribute]


def _notes(chart: Component) -> list[str]:
    return [str(node.children) for node in chart._traverse() if isinstance(node, dmc.Text)]


def test_a_scatter_is_a_point_per_run_at_its_latest_value_in_the_runs_color() -> None:
    chart = ScatterChart.render(ScatterSettings(x="lr", y="loss", x_scale="log"), _sweep())

    (trace,) = _figure(chart)["data"]
    assert (trace["x"], trace["y"]) == ([0.1, 0.01, 0.001], [1.0, 0.5, 0.8])
    assert trace["marker"]["color"] == [series_color(run) for run in (1, 2, 3)]
    assert trace["text"] == ["run-1", "run-2", "run-3"]
    assert _figure(chart)["layout"]["xaxis"]["type"] == "log"


def test_a_scatter_says_how_many_of_the_runs_shown_logged_both_axes() -> None:
    frame = _sweep()
    frame.loc[frame["run_id"] == 3, "loss"] = None

    assert any(
        "2 of the 3 runs shown logged both" in note
        for note in _notes(ScatterChart.render(ScatterSettings(x="lr", y="loss"), frame))
    )


def test_both_charts_read_a_row_per_run_never_a_history() -> None:
    scatter = ScatterChart.data_request(ScatterSettings(x="lr", y="loss"))
    every = ParallelCoordinatesChart.data_request(ParallelCoordinatesSettings(metric="loss"))
    named = ParallelCoordinatesChart.data_request(ParallelCoordinatesSettings(metric="loss", hparams=["lr"]))

    assert scatter.reductions.default is every.reductions.default is MetricReduction.LATEST
    assert (every.hparam_patterns, named.hparams, named.hparam_patterns) == (
        EVERY_KEY,
        frozenset({"lr"}),
        frozenset(),
    )


def test_parallel_coordinates_give_an_axis_to_each_hyperparameter_the_runs_do_not_share() -> None:
    chart = ParallelCoordinatesChart.render(ParallelCoordinatesSettings(metric="loss"), _sweep())

    (trace,) = _figure(chart)["data"]
    assert [axis["label"] for axis in trace["dimensions"]] == [
        "lr",
        "optimizer",
        "loss",
    ]  # not the shared seed
    optimizer: dict[str, Any] = trace["dimensions"][1]
    assert (optimizer["ticktext"], optimizer["values"]) == (["adam", "sgd"], [0, 1, 0])
    assert trace["line"]["color"] == [1.0, 0.5, 0.8]


@pytest.mark.parametrize(
    ("settings", "axes", "noted"),
    [
        pytest.param(
            ParallelCoordinatesSettings(metric="loss", hparams=["seed", "lr"]),
            ["seed", "lr"],
            None,
            id="named",
        ),
        pytest.param(
            ParallelCoordinatesSettings(metric="loss", max_axes=1),
            ["lr"],
            "1 more hyperparameters differ",
            id="capped",
        ),
    ],
)
def test_parallel_coordinates_draw_the_axes_asked_for_and_say_what_they_leave_out(
    settings: ParallelCoordinatesSettings, axes: list[str], noted: str | None
) -> None:
    chart = ParallelCoordinatesChart.render(settings, _sweep())

    (trace,) = _figure(chart)["data"]
    assert [axis["label"] for axis in trace["dimensions"]][:-1] == axes
    assert (
        any(noted in note for note in _notes(chart))
        if noted
        else not any("differ" in note for note in _notes(chart))
    )


def test_runs_that_share_every_hyperparameter_have_no_axes_to_draw() -> None:
    frame = _sweep()
    frame[hparam_column("lr")] = 0.1
    frame[hparam_column("optimizer")] = "adam"

    assert any(
        "share every hyperparameter" in note
        for note in _notes(ParallelCoordinatesChart.render(ParallelCoordinatesSettings(metric="loss"), frame))
    )
