"""Tests for the histogram chart: the arithmetic under it, and the figure each display hands Plotly."""

from __future__ import annotations

import itertools
import typing

import numpy as np
import pandas as pd
import pytest
from dash import dcc, html
from hypothesis import given
from hypothesis import strategies as st
from plotly.subplots import make_subplots

from dlboard.conftest import props
from dlboard.models import (
    RUN_NAME_COLUMN,
    ChartTypeRegistry,
    DataRequest,
    Histogram,
    RenderContext,
    histogram_column,
)
from dlboard.plugins.charts._histogram_math import common_edges, regrid, spread
from dlboard.plugins.charts.histogram import (
    Colorscale,
    HistogramChart,
    HistogramChartSettings,
    HistogramDisplay,
    _rows,
)
from dlboard.serve import series_swatch_class

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
    """The (name, swatch class) of each run label over the figure, in row order."""
    divs = [props(node) for node in chart._traverse() if isinstance(node, html.Div)]
    labels = [div for div in divs if "dl-histogram-run" in str(div.get("className"))]
    return [
        (str(label["children"]), str(label["className"]).removeprefix("dl-histogram-run "))
        for label in labels
    ]


def _values(figure: dict[str, typing.Any], trace: int, field: str) -> np.ndarray:
    """One field of one trace of `figure` as an array, a blank (null) as NaN."""
    values: np.ndarray = np.array(typing.cast("list[typing.Any]", figure["data"][trace][field]), dtype=float)
    return values


@given(st.integers(1, 40), st.floats(0, 1))
def test_a_figures_rows_are_laid_out_as_plotlys_subplots_would_lay_them_out(count: int, share: float) -> None:
    """Without building the figure Plotly builds to work it out, which was most of the chart's drawing time."""
    spacing = share / max(
        count - 1, 1
    )  # as much as Plotly allows: the rows' gaps fill at most the whole height

    expected: dict[str, typing.Any] = make_subplots(
        rows=count, cols=1, vertical_spacing=spacing
    ).to_plotly_json()["layout"]

    actual = _rows(count, spacing)

    assert actual.keys() == expected.keys()
    assert actual["template"] == expected["template"]
    for axis in (key for key in expected if key != "template"):
        assert actual[axis]["anchor"] == expected[axis]["anchor"]
        domain: list[float] = expected[axis]["domain"]
        assert actual[axis]["domain"] == pytest.approx(domain)


@st.composite
def _histograms(draw: st.DrawFn) -> Histogram:
    """Edges on a coarse lattice (so histograms share some), and counts with empty buckets among them."""
    edges = draw(st.lists(st.integers(-20, 20), min_size=2, max_size=8, unique=True).map(sorted))
    counts = draw(st.lists(st.integers(0, 50) | st.just(0), min_size=len(edges) - 1, max_size=len(edges) - 1))
    return Histogram(edges=[float(edge) for edge in edges], counts=counts)


@given(_histograms(), st.lists(st.floats(-25, 25), max_size=6))
def test_regrid_conserves_counts_and_is_exact_where_the_edges_line_up(
    histogram: Histogram, cuts: list[float]
) -> None:
    """Onto any edges that reach both of its ends; onto its own edges and more, each new bucket is a part of an old."""
    target = np.unique([*cuts, histogram.edges[0], histogram.edges[-1]])
    refined = np.unique([*target, *histogram.edges])

    assert regrid(histogram, target).sum() == pytest.approx(sum(histogram.counts))
    assert regrid(histogram, np.asarray(histogram.edges)).tolist() == pytest.approx(histogram.counts)
    regridded, lows = regrid(histogram, refined), refined[:-1]
    assert [
        regridded[(lows >= low) & (lows < high)].sum() for low, high in itertools.pairwise(histogram.edges)
    ] == pytest.approx(histogram.counts)


@given(st.lists(_histograms(), min_size=1, max_size=4), st.integers(2, 40))
def test_the_common_edges_are_every_histograms_edges_unless_capped_by_merging_neighbours(
    histograms: list[Histogram], max_rows: int
) -> None:
    """Histograms on one lattice add nothing; too many edges are merged down to `max_rows`, its ends kept."""
    every = sorted({edge for histogram in histograms for edge in histogram.edges})

    edges = common_edges(histograms, max_rows=max_rows).tolist()

    assert (edges[0], edges[-1]) == (every[0], every[-1])
    assert len(edges) - 1 <= max_rows
    assert edges == every or len(every) - 1 > max_rows


@given(_histograms())
def test_spread_reads_the_extremes_and_percentiles_off_the_buckets(histogram: Histogram) -> None:
    """In order, from the low edge of the first bucket with anything in it to the high edge of the last."""
    filled = [at for at, count in enumerate(histogram.counts) if count]

    low, p16, median, p84, high = spread(histogram, (0.16, 0.5, 0.84))

    if not filled:
        assert np.isnan([low, p16, median, p84, high]).all()
    else:
        assert (low, high) == (histogram.edges[filled[0]], histogram.edges[filled[-1] + 1])
        assert low <= p16 <= median <= p84 <= high


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
    assert _run_labels(chart) == [("run-1", series_swatch_class(1)), ("run-2", series_swatch_class(2))]
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

    assert _run_labels(chart) == [("run-2", series_swatch_class(2)), ("run-3", series_swatch_class(3))]
    assert "Showing the 2 most recent of 3 runs" in str(props(chart)["children"])


@pytest.mark.parametrize("df", [pd.DataFrame(), pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})])
def test_a_key_nothing_logged_says_so_instead_of_drawing(df: pd.DataFrame) -> None:
    chart = HistogramChart.render(_SETTINGS, df)

    assert f"No histograms logged for key '{_KEY}'" in str(props(chart)["children"])


def test_the_chart_asks_for_exactly_its_histogram_and_describes_its_settings() -> None:
    assert HistogramChart.data_request(_SETTINGS) == DataRequest(histograms=frozenset({_KEY}))
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
