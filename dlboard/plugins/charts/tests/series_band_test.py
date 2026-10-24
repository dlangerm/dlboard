"""The band chart: what it asks the panel for, and how it draws the bands the panel read."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pandas as pd
import pytest
from dash import dcc
from pydantic import ValidationError

from dlboard.models import MetricColumn, SeriesBand, SeriesBandRequest
from dlboard.models._panel_frame import SERIES_BAND_COLUMN_PREFIX
from dlboard.plugins.charts.series_band import SeriesBandChart, SeriesBandSettings
from dlboard.serve._pages._experiment._panel_data import build_series_band_dataframe

if TYPE_CHECKING:
    from dash.development.base_component import Component


def _figure(graph: dcc.Graph) -> dict[str, Any]:
    return cast("dict[str, Any]", graph.to_plotly_json()["props"]["figure"])


def test_the_band_asks_the_panel_for_the_patterns_it_spreads_on_its_grid_in_the_groups_it_names() -> None:
    settings = SeriesBandSettings(metrics=["layer_*/grad_norm"], grid=16, runs=2, group_by=1, max_groups=5)

    assert SeriesBandChart.hint_required_series(settings) == {
        SeriesBandRequest(
            patterns=frozenset({"layer_*/grad_norm"}), grid=16, runs=2, group_by=(1,), max_groups=5
        )
    }
    assert SeriesBandChart.hint_required_columns(settings) == set()


def test_a_band_with_no_metrics_asks_for_nothing() -> None:
    assert SeriesBandChart.hint_required_series(SeriesBandSettings()) == frozenset()


@pytest.mark.parametrize(
    "kwargs", [{"grid": 1}, {"grid": 65}, {"runs": 0}, {"runs": 4}, {"group_by": -1}, {"max_groups": 25}]
)
def test_the_band_bounds_its_grid_its_runs_and_its_groups(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SeriesBandSettings(**kwargs)


def _render(settings: SeriesBandSettings, bands: list[SeriesBand]) -> list[dict[str, Any]]:
    frame = build_series_band_dataframe(_request(settings), bands)
    graph = SeriesBandChart.render(settings, frame)
    assert isinstance(graph, dcc.Graph)
    return _figure(graph)["data"]


def _request(settings: SeriesBandSettings) -> SeriesBandRequest:
    (request,) = SeriesBandChart.hint_required_series(settings)
    return request


def test_a_band_draws_a_median_between_its_quartiles_inside_its_extremes_per_run() -> None:
    traces = _render(
        SeriesBandSettings(metrics=["m*"], grid=8, runs=1),
        [
            SeriesBand(1, "", 0, 4, 1.0, 2.0, 3.0, 4.0, 5.0, 3.0, "a", "e"),
            SeriesBand(1, "", 2, 4, 0.5, 1.5, 2.5, 3.5, 4.5, 2.5, "a", "e"),
        ],
    )
    medians = [trace for trace in traces if trace["line"].get("width") == 2]

    assert len(traces) == 5  # the extremes, the quartiles and the median, for one run
    assert medians[0]["y"] == [3.0, 2.5]
    assert medians[0]["x"] == [0, 2]


def test_hovering_a_step_names_the_keys_at_the_extremes_and_how_many_logged_a_value() -> None:
    (median,) = [
        trace
        for trace in _render(
            SeriesBandSettings(metrics=["m*"]),
            [SeriesBand(1, "", 0, 4, 1.0, 2.0, 3.0, 4.0, 5.0, 3.0, "low<b>", "high")],
        )
        if "text" in trace
    ]

    (hover,) = median["text"]
    assert "4 metrics" in hover
    assert "min 1 (low&lt;b&gt;)" in hover
    assert "max 5 (high)" in hover


def test_each_group_is_a_band_of_its_own_in_a_color_of_its_own() -> None:
    traces = _render(
        SeriesBandSettings(metrics=["acc/#/*"], group_by=1),
        [
            SeriesBand(1, "cat", 0, 2, 1.0, 1.0, 1.0, 3.0, 3.0, 2.0, "a", "b"),
            SeriesBand(1, "dog", 0, 2, 5.0, 5.0, 5.0, 7.0, 7.0, 6.0, "c", "d"),
        ],
    )
    medians = [trace for trace in traces if trace.get("name")]

    assert [trace["name"] for trace in medians] == ["cat", "dog"]
    assert medians[0]["line"]["color"] != medians[1]["line"]["color"]
    assert [trace["y"] for trace in medians] == [[1.0], [5.0]]


def test_the_mean_is_a_dashed_line_only_if_asked_for() -> None:
    bands = [SeriesBand(1, "", 0, 2, 1.0, 1.0, 1.0, 3.0, 3.0, 2.0, "a", "b")]

    plain = _render(SeriesBandSettings(metrics=["m*"]), bands)
    with_mean = _render(SeriesBandSettings(metrics=["m*"], show_mean=True), bands)

    assert not [trace for trace in plain if trace["line"].get("dash")]
    (mean,) = [trace for trace in with_mean if trace["line"].get("dash")]
    assert mean["y"] == [2.0]


def test_a_band_with_nothing_read_draws_no_traces() -> None:
    frame = pd.DataFrame({MetricColumn.RUN_ID: [1], MetricColumn.STEP: [0]})

    graph: Component = SeriesBandChart.render(SeriesBandSettings(metrics=["m*"]), frame)

    assert isinstance(graph, dcc.Graph)
    assert _figure(graph)["data"] == []


def test_a_band_column_is_named_by_what_it_asks_so_alike_charts_share_it() -> None:
    first = SeriesBandRequest(patterns=frozenset({"a*", "b"}), grid=8, runs=1)
    alike = SeriesBandRequest(patterns=frozenset({"b", "a*"}), grid=8, runs=1)
    other = SeriesBandRequest(patterns=frozenset({"a*", "b"}), grid=16, runs=1)

    assert first.column == alike.column != other.column
    assert first.column.startswith(SERIES_BAND_COLUMN_PREFIX)
