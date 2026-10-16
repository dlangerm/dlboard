"""Tests for the histogram chart: the arithmetic under it, and the figure each display hands Plotly."""

from __future__ import annotations

import typing

import numpy as np
import pandas as pd
import pytest
from dash import dcc, html

from dlboard.conftest import props
from dlboard.models import RUN_NAME_COLUMN, ChartTypeRegistry, Histogram, RenderContext, histogram_column
from dlboard.plugins.charts._histogram_math import common_edges, regrid, spread
from dlboard.plugins.charts.histogram import (
    Colorscale,
    HistogramChart,
    HistogramChartSettings,
    HistogramDisplay,
)

_KEY = "grad"
_SETTINGS = HistogramChartSettings(key=_KEY)


def _histogram(count: int = 10) -> Histogram:
    return Histogram(edges=[0.0, 1.0, 2.0, 4.0], counts=[count, 2 * count, 0])


def _frame(steps_by_run: dict[int, list[int]]) -> pd.DataFrame:
    """A panel dataframe with a histogram under `_KEY` at each of the given steps of each run."""
    return pd.DataFrame(
        [
            {
                "run_id": run_id,
                "step": step,
                histogram_column(_KEY): _histogram(step + 1),
                RUN_NAME_COLUMN: f"run-{run_id}",
            }
            for run_id, steps in steps_by_run.items()
            for step in steps
        ]
    )


def _figure(chart: typing.Any) -> dict[str, typing.Any]:  # noqa: ANN401
    graph = next(node for node in chart._traverse() if isinstance(node, dcc.Graph))
    return props(graph)["figure"]


def _run_labels(chart: typing.Any) -> list[tuple[str, str]]:  # noqa: ANN401
    """The (name, color class) of each run label over the figure, in row order."""
    divs = [props(node) for node in chart._traverse() if isinstance(node, html.Div)]
    labels = [div for div in divs if "dl-histogram-run" in str(div.get("className"))]
    return [(str(label["children"]), str(label["className"]).split()[-1]) for label in labels]


def _values(figure: dict[str, typing.Any], trace: int, field: str) -> np.ndarray:
    """One field of one trace of `figure` as an array, a blank (null) as NaN."""
    values: np.ndarray = np.array(typing.cast("list[typing.Any]", figure["data"][trace][field]), dtype=float)
    return values


def test_regrid_conserves_counts_and_is_exact_where_the_edges_line_up() -> None:
    histogram = _histogram()

    assert regrid(histogram, np.array([0.0, 1.0, 2.0, 4.0])).tolist() == [10, 20, 0]
    assert regrid(histogram, np.array([-5.0, 0.5, 1.5, 2.0, 9.0])).tolist() == pytest.approx([5, 15, 10, 0])


def test_the_common_edges_of_histograms_on_one_lattice_add_nothing() -> None:
    coarse, fine = _histogram(), Histogram(edges=[1.0, 2.0, 4.0], counts=[1, 1])

    assert common_edges([coarse, fine], max_rows=128).tolist() == [0.0, 1.0, 2.0, 4.0]


def test_the_common_edges_are_capped_by_merging_neighbours() -> None:
    wide = Histogram(edges=list(map(float, range(101))), counts=[1] * 100)

    edges = common_edges([wide], max_rows=10)

    assert len(edges) - 1 <= 10
    assert (edges[0], edges[-1]) == (0.0, 100.0)


def test_spread_reads_the_extremes_and_percentiles_off_the_buckets() -> None:
    histogram = Histogram(edges=[0.0, 1.0, 2.0, 3.0, 4.0], counts=[0, 10, 10, 0])

    low, p16, median, p84, high = spread(histogram, (0.16, 0.5, 0.84))

    assert (low, median, high) == (1.0, 2.0, 3.0)
    assert (p16, p84) == pytest.approx((1.32, 2.68))
    assert np.isnan(spread(Histogram(edges=[0.0, 1.0], counts=[0]), (0.16, 0.5, 0.84))).all()


@pytest.mark.parametrize("display", list(HistogramDisplay))
def test_every_display_is_one_figure_with_a_row_per_run_and_leaves_the_input_alone(
    display: HistogramDisplay,
) -> None:
    df = _frame({1: [0, 1, 2], 2: [0, 2]})
    before = df.copy()

    chart = HistogramChart.render(_SETTINGS.model_copy(update={"display": display}), df)
    figure = _figure(chart)

    pd.testing.assert_frame_equal(df, before)
    assert {"xaxis", "xaxis2", "yaxis", "yaxis2"} <= figure["layout"].keys()
    assert _run_labels(chart) == [("run-1", "dl-swatch-2"), ("run-2", "dl-swatch-3")]
    domains = [figure["layout"][axis]["domain"] for axis in ("yaxis", "yaxis2")]
    assert 0 <= domains[1][0] < domains[1][1] < domains[0][0] < domains[0][1] <= 1


def test_the_heatmap_shades_each_step_by_its_share_of_that_steps_values() -> None:
    figure = _figure(HistogramChart.render(_SETTINGS, _frame({1: [0, 1, 2]})))

    (trace,) = figure["data"]
    z = _values(figure, 0, "z")
    assert trace["x"] == [0, 1, 2]
    assert z.shape == (3, 3)
    assert np.nansum(z, axis=0) == pytest.approx([1.0, 1.0, 1.0])
    assert np.isnan(z[2]).all()  # the empty bucket is blank, not zero-coloured


def test_the_heatmap_can_show_raw_counts_instead() -> None:
    figure = _figure(
        HistogramChart.render(_SETTINGS.model_copy(update={"normalize": False}), _frame({1: [4]}))
    )

    assert np.nansum(_values(figure, 0, "z")) == 5 + 10


def test_bands_are_a_median_inside_nested_spreads_per_run() -> None:
    settings = _SETTINGS.model_copy(update={"display": HistogramDisplay.BANDS})

    figure = _figure(HistogramChart.render(settings, _frame({1: [0, 1], 2: [0, 1]})))

    assert len(figure["data"]) == 2 * 5
    highs, lows, p84, p16, median = (_values(figure, i, "y") for i in range(5))
    assert (highs >= p84).all()
    assert (p84 >= median).all()
    assert (median >= p16).all()
    assert (p16 >= lows).all()
    assert figure["data"][4]["x"] == [0, 1]


def test_the_step_view_has_a_frame_for_every_step_and_starts_on_the_latest() -> None:
    settings = _SETTINGS.model_copy(update={"display": HistogramDisplay.STEP})

    figure = _figure(HistogramChart.render(settings, _frame({1: [0, 1, 2], 2: [0, 2]})))

    assert [frame["name"] for frame in figure["frames"]] == ["0", "1", "2"]
    (slider,) = figure["layout"]["sliders"]
    assert slider["active"] == 2
    assert figure["layout"]["annotations"][-1]["text"] == "step 2"
    # Run 2 never logged step 1: it keeps showing its step 0 histogram there.
    assert figure["frames"][1]["data"][1]["y"] == figure["frames"][0]["data"][1]["y"]
    assert figure["data"][0]["y"] == figure["frames"][2]["data"][0]["y"]


def test_only_the_most_recent_runs_that_fit_the_page_are_drawn() -> None:
    chart = HistogramChart.render_in(_SETTINGS, _frame({1: [0], 2: [0], 3: [0]}), RenderContext(page_size=2))

    assert _run_labels(chart) == [("run-2", "dl-swatch-3"), ("run-3", "dl-swatch-4")]
    assert "Showing the 2 most recent of 3 runs" in str(props(chart)["children"])


@pytest.mark.parametrize("df", [pd.DataFrame(), pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})])
def test_a_key_nothing_logged_says_so_instead_of_drawing(df: pd.DataFrame) -> None:
    chart = HistogramChart.render(_SETTINGS, df)

    assert f"No histograms logged for key '{_KEY}'" in str(props(chart)["children"])


def test_the_chart_asks_for_exactly_its_histogram_and_describes_its_settings() -> None:
    assert HistogramChart.hint_required_histogram_keys(_SETTINGS) == {_KEY}
    assert HistogramChart.hint_required_columns(_SETTINGS) == set()
    assert HistogramChart.hint_required_artifact_keys(_SETTINGS) == set()
    assert HistogramChart.hint_required_hparams(_SETTINGS) == set()
    HistogramChart.register(allow_override=True)
    fields = ChartTypeRegistry.get_registered_chart_types()[HistogramChart.name]
    assert fields["display"].choices == ("heatmap", "bands", "step")
    assert fields["key"].column_kind == "histogram"
    assert HistogramChart.natural_width(_SETTINGS.model_copy(update={"width": 640})) == 640


def test_max_rows_and_max_slider_steps_cap_what_a_figure_carries() -> None:
    wide = pd.DataFrame(
        {
            "run_id": 1,
            "step": step,
            histogram_column(_KEY): Histogram(edges=list(map(float, range(51))), counts=[step + 1] * 50),
        }
        for step in range(20)
    )

    heatmap = _figure(HistogramChart.render(_SETTINGS.model_copy(update={"max_rows": 10}), wide))
    slider = _figure(
        HistogramChart.render(
            _SETTINGS.model_copy(update={"display": HistogramDisplay.STEP, "max_slider_steps": 5}), wide
        )
    )

    assert len(typing.cast("list[object]", heatmap["data"][0]["z"])) <= 10
    assert len(slider["frames"]) == 5


def test_bands_name_their_percentiles_after_band_share_and_take_their_color_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DLBOARD_HISTOGRAM_ACCENT_COLOR", "#ff0000")
    settings = _SETTINGS.model_copy(update={"display": HistogramDisplay.BANDS, "band_share": 0.5})

    figure = _figure(HistogramChart.render(settings, _frame({1: [0, 1]})))

    median: dict[str, typing.Any] = figure["data"][4]
    assert median["line"]["color"] == "rgba(255, 0, 0, 1)"
    assert "25%" in median["text"][0]
    assert "75%" in median["text"][0]


@pytest.mark.parametrize(
    "colorscale",
    [
        Colorscale.VIRIDIS,
        Colorscale.CIVIDIS,
        Colorscale.PLASMA,
        Colorscale.INFERNO,
        Colorscale.MAGMA,
        Colorscale.BLUES,
    ],
)
def test_every_colorscale_is_one_plotly_draws(colorscale: Colorscale) -> None:
    settings = _SETTINGS.model_copy(update={"colorscale": colorscale})

    figure = _figure(HistogramChart.render(settings, _frame({1: [0, 1]})))

    assert figure["data"][0]["colorscale"]
