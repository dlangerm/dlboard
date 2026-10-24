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


def test_the_band_asks_the_panel_for_the_patterns_it_spreads_on_its_grid() -> None:
    settings = SeriesBandSettings(metrics=["layer_*/grad_norm"], grid=16, runs=2)

    assert SeriesBandChart.hint_required_series(settings) == SeriesBandRequest(
        patterns=frozenset({"layer_*/grad_norm"}), grid=16, runs=2
    )
    assert SeriesBandChart.hint_required_columns(settings) == set()


def test_a_band_with_no_metrics_asks_for_nothing() -> None:
    assert SeriesBandChart.hint_required_series(SeriesBandSettings()) is None


@pytest.mark.parametrize("kwargs", [{"grid": 1}, {"grid": 65}, {"runs": 0}, {"runs": 4}])
def test_the_band_bounds_its_grid_and_its_runs(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SeriesBandSettings(**kwargs)


def test_a_band_draws_a_median_between_its_quartiles_inside_its_extremes_per_run() -> None:
    request = SeriesBandRequest(patterns=frozenset({"m*"}), grid=8, runs=1)
    bands = [
        SeriesBand(run_id=1, step=0, keys=4, minimum=1.0, lower=2.0, median=3.0, upper=4.0, maximum=5.0),
        SeriesBand(run_id=1, step=2, keys=4, minimum=0.5, lower=1.5, median=2.5, upper=3.5, maximum=4.5),
    ]
    frame = build_series_band_dataframe(request, bands)

    graph = SeriesBandChart.render(SeriesBandSettings(metrics=["m*"], grid=8, runs=1), frame)
    assert isinstance(graph, dcc.Graph)
    traces: list[dict[str, Any]] = _figure(graph)["data"]
    medians = [trace for trace in traces if trace["line"].get("width") == 2]

    assert len(traces) == 5  # minimum, maximum, lower, upper and median, for one run
    assert medians[0]["y"] == [3.0, 2.5]
    assert medians[0]["x"] == [0, 2]


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
