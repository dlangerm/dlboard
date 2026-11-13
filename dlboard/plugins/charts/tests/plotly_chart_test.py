"""Tests for the Plotly chart: what the browser is handed to fetch and draw, a figure per step or a template's data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest
from pydantic import ValidationError

from dlboard import models
from dlboard.conftest import find_all_props
from dlboard.models import (
    EXPERIMENT_COLUMN,
    PLOTLY_FIGURE_EXTENSION,
    RUN_NAME_COLUMN,
    Artifact,
    ArtifactWindow,
    ChartInstance,
    DataRequest,
    RenderContext,
    WindowAt,
    artifact_column,
    artifact_logged_column,
    plotly_data_key,
)
from dlboard.plugins.charts.plotly_chart import (
    COMBINED_RUNS,
    DLBOARD_THEME,
    PLOTLY_WINDOW_STEPS,
    PlotlyChart,
    PlotlySettings,
    StreamMode,
    _LoadedWindow,
    read_window,
)
from dlboard.serve import series_color, series_swatch_class

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

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


@pytest.mark.parametrize(
    ("mode", "data_window"),
    [
        (StreamMode.SCRUB, ArtifactWindow(plotly_data_key("pr"), PLOTLY_WINDOW_STEPS)),
        (StreamMode.LATEST, ArtifactWindow(plotly_data_key("pr"), 1, at=WindowAt.LATEST)),
        (StreamMode.ACCUMULATE, ArtifactWindow(plotly_data_key("pr"), 7, at=WindowAt.THINNED)),
    ],
    ids=["a-window-at-a-time", "each-runs-latest", "each-run-thinned"],
)
def test_the_chart_reads_a_window_of_its_figures_and_the_data_logged_into_it_its_mode_draws(
    mode: StreamMode, data_window: ArtifactWindow
) -> None:
    """Neither read grows with the steps logged (#619)."""
    assert PlotlyChart.data_request(
        PlotlySettings(key="pr", data_steps=mode, max_data_steps=7)
    ) == DataRequest(
        artifacts=frozenset({"pr"}),
        artifact_prefixes=frozenset({plotly_data_key("pr")}),
        artifact_windows=frozenset({ArtifactWindow("pr", PLOTLY_WINDOW_STEPS), data_window}),
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


@pytest.mark.parametrize(("most", "noted"), [(2, True), (5, False)], ids=["thinned", "drawn-whole"])
def test_a_chart_that_thinned_a_runs_data_says_so(most: int, noted: bool) -> None:
    stack = PlotlyChart.render(
        PlotlySettings(key="gps", max_data_steps=most), _template_frame({1: [1, 2, 3, 4, 5]})
    )

    assert bool(find_all_props(stack, lambda p: "max data steps" in str(p.get("children")))) is noted


# ---- read through windows (#619): every step logged, and the URLs of some ----


def _windowed_frame(logged: dict[int, list[int]], read: set[int]) -> pd.DataFrame:
    """A panel's frame of figures under `pr` read through a window: each run's every step, the URLs of `read` alone."""
    return pd.DataFrame(
        [
            {
                "run_id": run,
                "step": step,
                EXPERIMENT_COLUMN: 7,
                artifact_logged_column("pr"): True,
                **({artifact_column("pr"): f"/artifact/{run}-{step}"} if step in read else {}),
            }
            for run, steps in logged.items()
            for step in steps
        ]
    )


def test_a_windowed_charts_slider_goes_to_every_step_and_every_run_is_drawn() -> None:
    stack = PlotlyChart.render(PlotlySettings(key="pr"), _windowed_frame({1: [0, 1, 2, 3], 2: [3]}, {0, 1}))

    payload = _payload(stack)
    assert payload["steps"] == [0, 1, 2, 3]
    assert payload["runs"]["1"]["urls"] == {"0": "/artifact/1-0", "1": "/artifact/1-1"}
    # Run 2 logged nothing in the window yet: drawn once the slider gets to its steps.
    assert payload["runs"]["2"]["urls"] == {}
    [window] = _of_type(stack, "plotly-chart-window")
    assert window["data"] == {"first": 0, "last": 1, "runs": {"7": [1, 2]}, "key": "pr"}


def test_a_windowed_template_that_thinned_a_runs_data_says_so() -> None:
    data_key = plotly_data_key("gps")
    frame = pd.DataFrame(
        [
            {"run_id": 1, "step": 0, artifact_column("gps"): "/artifact/t1"},
            *(
                {
                    "run_id": 1,
                    "step": step,
                    artifact_logged_column(data_key): True,
                    # The panel read every 4th step's data of the 10 logged, and the latest.
                    **({artifact_column(data_key): f"/artifact/d{step}"} if step in {1, 5, 9, 10} else {}),
                }
                for step in range(1, 11)
            ),
        ]
    )

    stack = PlotlyChart.render(PlotlySettings(key="gps", max_data_steps=3), frame)

    assert _payload(stack)["runs"]["1"]["urls"] == {str(s): f"/artifact/d{s}" for s in (1, 5, 9, 10)}
    assert find_all_props(stack, lambda p: "max data steps" in str(p.get("children")))


@pytest.mark.usefixtures("_plain_artifact_urls")
def test_the_slider_leaving_the_window_reads_the_figures_around_where_it_went(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_artifact_refs(
        models.Artifact(
            key="pr", fname="f.json", run_id=run.id, experiment_id=experiment_id, step=step, ref=f"r://{step}"
        )
        for step in range(3 * PLOTLY_WINDOW_STEPS)
    )
    window = _LoadedWindow(first=0, last=PLOTLY_WINDOW_STEPS - 1, runs={experiment_id: [run.id]}, key="pr")

    read = read_window(store, window, 2 * PLOTLY_WINDOW_STEPS)

    assert read is not None
    patch, moved = read
    [operation] = patch.to_plotly_json()["operations"]
    assert operation["location"] == ["runs", str(run.id), "urls"]
    urls: dict[str, str] = operation["params"]["value"]
    assert len(urls) == PLOTLY_WINDOW_STEPS
    assert moved.first <= 2 * PLOTLY_WINDOW_STEPS <= moved.last
    assert sorted(map(int, urls)) == list(range(moved.first, moved.last + 1))
