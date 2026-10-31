"""A comparison in a real browser: runs of two experiments in one chart, taken out of it, hidden from it, and its copy."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import pandas as pd
import pendulum
import pytest
from playwright.sync_api import expect

from dlboard import models
from dlboard.models import ChartInstance, PanelInstance
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve import get_system_data_store
from dlboard.serve._pages._experiment._page_model import BasicExperimentPage

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dash import Dash
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser

_LEGEND = ".mantine-ChartLegend-legendItemName"
_ROWS = "#comparison-members-grid .ag-center-cols-container .ag-row"
_CHECKBOX = ".ag-selection-checkbox input"


@pytest.fixture(autouse=True)
def _session_app_is_current(dlboard_app: Dash) -> Iterator[None]:
    """Read stores off `dlboard_app` in this module's helpers, as `browser_test.py` does."""
    with dlboard_app.server.app_context():
        yield


class _Comparison(NamedTuple):
    id: int
    project_id: int
    run_ids: list[int]


def _a_shared_comparison_of_two_experiments(name: str) -> _Comparison:
    """A project's "baseline" and "sweep", each with a run called "fluffy", compared in a loss chart by someone else."""
    store = get_system_data_store()
    project = store.create_project(models.NewProject(name=name, description=""))
    run_ids: list[int] = []
    for offset, experiment_name in enumerate(("baseline", "sweep")):
        experiment = store.create_experiment(
            models.NewExperiment(project_id=project.id, name=experiment_name)
        )
        run = store.create_run(models.NewRun(experiment_id=experiment.id, name="fluffy"))
        store.log_metrics(
            models.LoggedMetrics(
                metrics={"loss": 1.0 / (step + 1 + offset)},
                step=step,
                experiment_id=experiment.id,
                run_id=run.id,
                timestamp_utc=pendulum.now("UTC"),
            )
            for step in range(3)
        )
        run_ids.append(run.id)
    owner = store.get_or_create_user(models.Principal.unverified("owner"))
    LineChart.register(allow_override=True)
    panel = PanelInstance[pd.DataFrame, object](
        name="loss",
        charts=[ChartInstance(chart_type=LineChart.name, parameters={"column": "loss", "x_axis": "step"})],
    )
    created = store.create_comparison(
        BasicExperimentPage,
        models.NewPage[Any, Any](
            project_id=project.id, owner_id=owner.id, name="Sweep vs baseline", shared=True, panels=[panel]
        ),
        frozenset(run_ids),
    )
    return _Comparison(created.id, project.id, run_ids)


def test_a_comparison_draws_runs_of_two_experiments_in_one_chart_with_names_that_say_whose_they_are(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    comparison = _a_shared_comparison_of_two_experiments("Overlaid Project")

    page.goto(f"{live_server_url}/comparison/{comparison.id}")

    expect(page.get_by_role("heading", name="Sweep vs baseline")).to_be_visible()
    expect(page.get_by_text("Your edits save to a copy of your own")).to_be_visible()
    expect(page.locator(_ROWS)).to_have_count(2)
    expect(page.locator(_LEGEND)).to_have_count(2)
    assert set(page.locator(_LEGEND).all_text_contents()) == {"baseline / fluffy", "sweep / fluffy"}
    assert console_errors == []


def test_a_run_taken_out_of_a_shared_comparison_leaves_its_chart_and_forks_nothing(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    comparison = _a_shared_comparison_of_two_experiments("Removed Project")
    page.goto(f"{live_server_url}/comparison/{comparison.id}")
    expect(page.locator(_LEGEND)).to_have_count(2)

    page.locator(_ROWS).first.locator(_CHECKBOX).check()
    page.locator("#run-list-bar-comparison").get_by_role("button", name="Remove").click()

    expect(page.locator(_ROWS)).to_have_count(1)
    expect(page.locator(_LEGEND)).to_have_count(1)
    assert page.url.endswith(
        f"/comparison/{comparison.id}"
    )  # not forked: the runs of a shared one are any editor's
    store = get_system_data_store()
    assert [run.id for run in store.comparison_runs(comparison.id)] == [comparison.run_ids[0]] or [
        run.id for run in store.comparison_runs(comparison.id)
    ] == [comparison.run_ids[1]]
    assert console_errors == []


def test_hiding_a_run_is_the_viewers_own_and_saves_no_copy_of_a_shared_comparison(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    comparison = _a_shared_comparison_of_two_experiments("Hidden Project")
    page.goto(f"{live_server_url}/comparison/{comparison.id}")
    expect(page.locator(_LEGEND)).to_have_count(2)

    page.locator(_ROWS).first.get_by_role("button", name=re.compile(r"^Hide")).click()

    expect(page.locator(_LEGEND)).to_have_count(1)
    expect(page.locator(_ROWS)).to_have_count(2)
    expect(page.get_by_text("1 hidden")).to_be_visible()
    expect(page).to_have_url(re.compile(rf"/comparison/{comparison.id}\?.*hidden="))  # a link carries it
    original = get_system_data_store().get_view(BasicExperimentPage, comparison.id)
    assert original is not None
    assert cast("BasicExperimentPage", original).document.excluded_runs == []
    page.reload()  # and so does a reload
    expect(page.locator(_LEGEND)).to_have_count(1)
    assert console_errors == []


def test_a_panel_edit_of_someone_elses_comparison_saves_a_copy_of_your_own_that_keeps_what_you_hid(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    comparison = _a_shared_comparison_of_two_experiments("Forked Project")
    page.goto(f"{live_server_url}/comparison/{comparison.id}")
    page.locator(_ROWS).first.locator(".dl-eye").click()
    expect(page.locator(_LEGEND)).to_have_count(1)

    page.locator(".dl-panel-item-header").first.hover()
    page.get_by_role("button", name="Panel actions").first.click()
    page.get_by_role("menuitem", name="Rename panel…").click()
    page.get_by_role("textbox", name="Panel name", exact=True).fill("renamed")
    page.get_by_role("button", name="Save", exact=True).click()

    # The edit branched into a copy of the viewer's own, which the page then follows to, still hiding the same run.
    expect(page).to_have_url(re.compile(rf"/comparison/(?!{comparison.id}[?]|{comparison.id}$)\d+.*hidden="))
    expect(page.get_by_role("heading", name="My comparison")).to_be_visible()
    expect(page.locator(_ROWS)).to_have_count(2)
    expect(page.locator(_LEGEND)).to_have_count(1)
    assert console_errors == []


def test_every_run_pinned_in_a_colour_is_added_to_a_shared_comparison_by_anyone_who_can_edit_it(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    comparison = _a_shared_comparison_of_two_experiments("Added Project")
    store = get_system_data_store()
    (experiment,) = (e for e in store.get_experiments(comparison.project_id) if e.name == "baseline")
    red, green = (
        store.create_run(models.NewRun(experiment_id=experiment.id, name=name)).id
        for name in ("red-one", "green-one")
    )
    pinner = store.get_or_create_user(models.Principal.unverified("pinner")).id
    store.pin_runs(pinner, frozenset({red}), models.PinColor.RED)
    store.pin_runs(pinner, frozenset({green}), models.PinColor.GREEN)
    page.goto(f"{live_server_url}/comparison/{comparison.id}")
    expect(page.locator(_ROWS)).to_have_count(2)

    page.get_by_role("button", name="Add runs").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("checkbox", name="Pinned red").locator("xpath=following-sibling::label").click()
    expect(dialog.get_by_text("1 run pinned in these colours")).to_be_visible()
    dialog.get_by_role("button", name="Add pinned runs").click()

    expect(page.get_by_text("Added 1 run.")).to_be_visible()
    expect(page.locator(_ROWS)).to_have_count(3)
    expect(page.locator(_ROWS).filter(has_text="red-one")).to_have_count(1)
    expect(page.locator(_ROWS).filter(has_text="green-one")).to_have_count(0)
    assert page.url.endswith(
        f"/comparison/{comparison.id}"
    )  # the runs of a shared comparison are any editor's: no copy
    assert {run.name for run in store.comparison_runs(comparison.id)} == {"fluffy", "red-one"}
    assert console_errors == []


def test_the_bar_hides_shows_and_pins_the_selected_runs_of_a_comparison(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    comparison = _a_shared_comparison_of_two_experiments("Bar Project")
    page.goto(f"{live_server_url}/comparison/{comparison.id}")
    rows = page.locator(_ROWS)
    bar = page.locator("#run-list-bar-comparison")
    expect(rows).to_have_count(2)
    expect(page.locator(_LEGEND)).to_have_count(2)
    rows.nth(0).locator(_CHECKBOX).check()
    rows.nth(1).locator(_CHECKBOX).check()

    bar.get_by_role("button", name="Hide").click()
    expect(page.locator(_LEGEND)).to_have_count(0)
    expect(page.get_by_text("2 · 2 hidden")).to_be_visible()
    bar.get_by_role("button", name="Show").click()
    expect(page.locator(_LEGEND)).to_have_count(2)
    # The runs stayed selected through both, so the bar still acts on them.
    bar.get_by_role("button", name="Pin", exact=True).click()
    page.get_by_role("menuitem", name="Pin green").click()
    expect(rows.nth(0).locator(".dl-pins .dl-icon-pin:not(.dl-pin-ghost)")).to_have_count(1)
    expect(rows.nth(1).locator(".dl-pins .dl-icon-pin:not(.dl-pin-ghost)")).to_have_count(1)
    assert console_errors == []


def test_a_comparisons_runs_are_listed_in_the_sidebar_with_the_experiment_each_is_of(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    comparison = _a_shared_comparison_of_two_experiments("Sidebar Project")

    page.goto(f"{live_server_url}/comparison/{comparison.id}")

    expect(page.locator(_ROWS)).to_have_count(2)
    expect(page.locator(_ROWS).filter(has_text="baseline")).to_have_count(1)
    expect(page.locator(_ROWS).filter(has_text="sweep")).to_have_count(1)
    expect(page.get_by_role("button", name="Add runs")).to_be_visible()
    # A link to one of them goes to the run in its own experiment.
    page.locator(_ROWS).filter(has_text="sweep").get_by_role("link", name="fluffy").click()
    expect(page).to_have_url(re.compile(r"/experiment/\d+\?run=\d+"))
    expect(page.locator("#focus-banner")).to_contain_text("fluffy")
    assert console_errors == []
