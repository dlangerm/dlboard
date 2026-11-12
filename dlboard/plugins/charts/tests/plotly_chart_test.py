"""Tests for the Plotly chart: what the browser is handed to fetch and draw, a figure per step or a template's data."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from dlboard.conftest import find_all_props
from dlboard.models import (
    PLOTLY_FIGURE_EXTENSION,
    RUN_NAME_COLUMN,
    Artifact,
    ChartInstance,
    DataRequest,
    RenderContext,
    artifact_column,
    plotly_data_key,
)
from dlboard.plugins.charts.plotly_chart import (
    COMBINED_RUNS,
    DLBOARD_THEME,
    PlotlyChart,
    PlotlySettings,
    StreamMode,
    thinned,
)
from dlboard.serve import series_color, series_swatch_class

PlotlyChart.register(allow_override=True)


def _frame(runs: int, steps: int, *, named: bool = True) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "run_id": run_id,
                "step": step * 10,
                artifact_column("pr"): f"/artifact/{run_id}-{step}",
                **({RUN_NAME_COLUMN: f"run-{run_id}"} if named else {}),
            }
            for run_id in range(1, runs + 1)
            for step in range(steps)
        ]
    )


def _of_type(component: object, kind: str) -> list[dict[str, Any]]:
    return find_all_props(component, lambda p: isinstance(p.get("id"), dict) and p["id"].get("type") == kind)


def _payload(component: object) -> dict[str, Any]:
    [store] = _of_type(component, "plotly-chart-data")
    return store["data"]


def test_the_browser_is_handed_each_runs_figure_url_by_step() -> None:
    payload = _payload(PlotlyChart.render(PlotlySettings(key="pr"), _frame(runs=2, steps=2)))

    assert payload["order"] == [1, 2]
    assert payload["steps"] == [0, 10]
    assert payload["runs"]["1"] == {
        "name": "run-1",
        "swatch": series_swatch_class(1),
        "color": series_color(1),
        "urls": {"0": "/artifact/1-0", "10": "/artifact/1-1"},
    }
    assert payload["template"] is None


def test_a_run_with_no_name_is_called_by_its_id() -> None:
    payload = _payload(PlotlyChart.render(PlotlySettings(key="pr"), _frame(runs=1, steps=1, named=False)))

    assert payload["runs"]["1"]["name"] == "Run 1"


@pytest.mark.parametrize(("steps", "sliders"), [(1, 0), (3, 1)], ids=["logged-once", "logged-at-steps"])
def test_a_slider_scrubs_the_steps_only_when_there_is_more_than_one(steps: int, sliders: int) -> None:
    stack = PlotlyChart.render(PlotlySettings(key="pr"), _frame(runs=1, steps=steps))

    assert len(_of_type(stack, "plotly-chart-slider")) == sliders
    [step] = _of_type(stack, "plotly-chart-step")
    assert step["data"] == 0


@pytest.mark.parametrize(
    ("combine", "graphs"),
    [(False, ["1", "2", "3"]), (True, [COMBINED_RUNS])],
    ids=["a-figure-per-run", "combined"],
)
def test_runs_are_drawn_a_figure_each_or_all_in_one(combine: bool, graphs: list[str]) -> None:
    stack = PlotlyChart.render(PlotlySettings(key="pr", combine_runs=combine), _frame(runs=3, steps=1))

    assert [graph["id"]["run"] for graph in _of_type(stack, "plotly-chart-graph")] == graphs


def test_figures_per_run_are_paged_by_the_page_size() -> None:
    stack = PlotlyChart.render_in(
        PlotlySettings(key="pr"), _frame(runs=5, steps=1), RenderContext(page_size=2)
    )

    pages = _of_type(stack, "plotly-chart-page")
    assert [len(page["children"]) for page in pages] == [2, 2, 1]
    assert len(_of_type(stack, "plotly-chart-pager")) == 1


@pytest.mark.parametrize(
    ("own_theme", "theme"), [(False, DLBOARD_THEME), (True, None)], ids=["page-theme", "figure-theme"]
)
def test_a_figure_is_drawn_in_the_page_theme_unless_asked_not_to(own_theme: bool, theme: object) -> None:
    payload = _payload(
        PlotlyChart.render(PlotlySettings(key="pr", use_figure_theme=own_theme), _frame(runs=1, steps=1))
    )

    assert payload["theme"] == theme


@pytest.mark.parametrize(
    "frame",
    [
        pd.DataFrame({"run_id": [1], "step": [0]}),
        pd.DataFrame({"run_id": [1], "step": [0], artifact_column("pr"): [None]}),
    ],
    ids=["key-never-logged", "no-figure-for-the-runs-shown"],
)
def test_a_chart_with_nothing_logged_says_so(frame: pd.DataFrame) -> None:
    stack = PlotlyChart.render(PlotlySettings(key="pr"), frame)

    assert not _of_type(stack, "plotly-chart-graph")
    assert find_all_props(stack, lambda p: "Nothing logged for pr" in str(p.get("children")))


def test_the_chart_reads_its_figure_and_any_data_logged_into_it() -> None:
    assert PlotlyChart.data_request(PlotlySettings(key="pr")) == DataRequest(
        artifacts=frozenset({"pr"}), artifact_prefixes=frozenset({plotly_data_key("pr")})
    )


@pytest.mark.parametrize(
    ("fname", "displays"),
    [(f"abc{PLOTLY_FIGURE_EXTENSION}", True), ("abc.png", False), ("abc.plotly-data.json", False)],
    ids=["figure", "image", "template-data"],
)
def test_it_displays_logged_figures_only(fname: str, displays: bool) -> None:
    artifact = Artifact(key="k", fname=fname, run_id=1, experiment_id=1, step=0, ref="file:///k")

    assert PlotlyChart.can_display_artifact(artifact) is displays


@pytest.mark.parametrize(
    ("settings", "context", "width"),
    [
        (PlotlySettings(key="pr", height=300), RenderContext(columns=3), 1200),
        (PlotlySettings(key="pr", height=300, combine_runs=True), RenderContext(columns=3), 400),
        (PlotlySettings(key="pr", width=640), RenderContext(columns=3), 640),
    ],
    ids=["a-figure-per-column", "combined", "set-width"],
)
def test_the_chart_is_as_wide_as_its_figures(
    settings: PlotlySettings, context: RenderContext, width: int
) -> None:
    assert PlotlyChart.natural_width_in(settings, context) == width


def test_settings_without_a_key_are_invalid() -> None:
    with pytest.raises(ValidationError):
        ChartInstance(chart_type=PlotlyChart.name, parameters={}).render(_frame(runs=1, steps=1))


# ---- a template: a figure logged once, drawn with the data logged into it ----


def _template_frame(data_steps: dict[int, list[int]], template_runs: tuple[int, ...] = (1,)) -> pd.DataFrame:
    """A frame of the template `gps` logged by `template_runs` at step 0, and each run's data at its steps."""
    return pd.DataFrame(
        [
            *(
                {"run_id": run, "step": 0, artifact_column("gps"): f"/artifact/t{run}"}
                for run in template_runs
            ),
            *(
                {
                    "run_id": run,
                    "step": step,
                    artifact_column(plotly_data_key("gps")): f"/artifact/d{run}-{step}",
                }
                for run, steps in data_steps.items()
                for step in steps
            ),
        ]
    )


@pytest.mark.parametrize(
    ("mode", "urls", "steps"),
    [
        (StreamMode.ACCUMULATE, {"1": "/artifact/d1-1", "2": "/artifact/d1-2", "3": "/artifact/d1-3"}, [3]),
        (StreamMode.LATEST, {"3": "/artifact/d1-3"}, [3]),
        (StreamMode.SCRUB, {"1": "/artifact/d1-1", "2": "/artifact/d1-2", "3": "/artifact/d1-3"}, [1, 2, 3]),
    ],
    ids=["every-step", "latest", "one-step-at-a-time"],
)
def test_a_template_is_drawn_with_the_data_of_the_steps_its_mode_asks_for(
    mode: StreamMode, urls: dict[str, str], steps: list[int]
) -> None:
    stack = PlotlyChart.render(PlotlySettings(key="gps", data_steps=mode), _template_frame({1: [1, 2, 3]}))

    payload = _payload(stack)
    assert payload["template"] == "/artifact/t1"
    assert payload["data_steps"] == mode
    assert payload["runs"]["1"]["urls"] == urls
    assert payload["steps"] == steps
    # Only a template scrubbed a step at a time has a slider.
    assert len(_of_type(stack, "plotly-chart-slider")) == (mode is StreamMode.SCRUB)


def test_the_template_is_the_newest_runs_and_every_run_with_data_is_drawn() -> None:
    payload = _payload(
        PlotlyChart.render(PlotlySettings(key="gps"), _template_frame({1: [1], 2: [1], 3: [1]}, (1, 2)))
    )

    assert payload["template"] == "/artifact/t2"
    assert payload["order"] == [1, 2, 3]


def test_data_with_no_template_logged_draws_nothing() -> None:
    stack = PlotlyChart.render(PlotlySettings(key="gps"), _template_frame({1: [1]}, template_runs=()))

    assert not _of_type(stack, "plotly-chart-graph")


@pytest.mark.parametrize(
    ("logged", "most", "kept"),
    [
        (list(range(5)), 8, list(range(5))),
        (list(range(10)), 4, [0, 4, 8, 9]),
        (list(range(11)), 4, [0, 4, 8, 10]),
        (list(range(0, 100, 10)), 3, [0, 40, 80, 90]),
    ],
    ids=["few-enough", "every-4th-and-the-last", "the-last-is-its-own", "by-position-not-value"],
)
def test_long_data_is_thinned_by_a_power_of_two_keeping_the_latest(
    logged: list[int], most: int, kept: list[int]
) -> None:
    assert thinned(logged, most) == kept


def test_the_steps_kept_stay_put_as_a_run_logs_more() -> None:
    before, after = thinned(list(range(20)), 8), thinned(list(range(24)), 8)

    assert before[:-1] == after[: len(before) - 1]


@pytest.mark.parametrize(("most", "noted"), [(2, True), (5, False)], ids=["thinned", "drawn-whole"])
def test_a_chart_that_thinned_a_runs_data_says_so(most: int, noted: bool) -> None:
    stack = PlotlyChart.render(
        PlotlySettings(key="gps", max_data_steps=most), _template_frame({1: [1, 2, 3, 4, 5]})
    )

    assert bool(find_all_props(stack, lambda p: "max data steps" in str(p.get("children")))) is noted
