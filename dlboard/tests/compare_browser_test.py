"""Getting from runs to a comparison in a real browser: from an experiment, from the project's pinned runs, and from a pin."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pendulum
import pytest
from playwright.sync_api import expect

from dlboard import models
from dlboard._identity import resolve_username
from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.serve import get_system_data_store
from dlboard.serve._pages._experiment._page_model import BasicExperimentPage
from dlboard.serve._pages._experiment._runs_table import NAVBAR_HPARAM_DATATABLE_ID
from dlboard.tests.browser_test import _auto_generate_charts, _create_project_and_experiment, _log_runs
from dlboard.tests.comparison_browser_test import _a_shared_comparison_of_two_experiments

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dash import Dash
    from playwright.sync_api import Locator, Page

pytestmark = pytest.mark.browser

_CHECKBOX = ".ag-selection-checkbox input"
_LEGEND = ".mantine-ChartLegend-legendItemName"
_MEMBERS = "#comparison-members-grid .ag-center-cols-container .ag-row"


@pytest.fixture(autouse=True)
def _session_app_is_current(dlboard_app: Dash) -> Iterator[None]:
    """Read stores off `dlboard_app` in this module's helpers, as `browser_test.py` does."""
    with dlboard_app.server.app_context():
        yield


def _rows(page: Page) -> Locator:
    return page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID} .ag-center-cols-container .ag-row")


def _an_experiment_with_runs(page: Page, live_server_url: str, name: str, count: int) -> int:
    """The experiment the page is on after creating it, with `count` runs and its charts drawn: its id."""
    _create_project_and_experiment(page, live_server_url, name)
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    _log_runs(BasicDlboardAPI(live_server_url), experiment_id, count)
    page.reload()
    _auto_generate_charts(page)
    return experiment_id


def test_pinning_runs_of_two_experiments_and_comparing_the_pins_is_one_menu_away(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    first = _an_experiment_with_runs(page, live_server_url, "Pin Then Compare", 1)
    store = get_system_data_store()
    project_id = store.get_experiment(first).project_id  # pyrefly: ignore [missing-attribute]
    second = store.create_experiment(models.NewExperiment(project_id=project_id, name="the sibling"))
    sibling_run = store.create_run(models.NewRun(experiment_id=second.id, name="sibling-run"))
    store.log_metrics(
        models.LoggedMetrics(
            metrics={"loss": 1.0},
            step=step,
            experiment_id=second.id,
            run_id=sibling_run.id,
            timestamp_utc=pendulum.now("UTC"),
        )
        for step in range(3)
    )

    # Pin a run here, and one in the sibling experiment, in the same colour.
    _rows(page).first.locator(_CHECKBOX).check()
    page.locator("#run-list-bar-experiment").get_by_role("button", name="Pin", exact=True).click()
    page.get_by_role("menuitem", name="Pin red").click()
    expect(page.get_by_text("Pinned 1 run")).to_be_visible()
    page.goto(f"{live_server_url}/experiment/{second.id}")
    _rows(page).first.locator(_CHECKBOX).check()
    page.locator("#run-list-bar-experiment").get_by_role("button", name="Pin", exact=True).click()
    page.get_by_role("menuitem", name="Pin red").click()
    expect(page.get_by_text("Pinned 1 run")).to_be_visible()

    # Without leaving the page: compare what is pinned red.
    page.get_by_role("button", name="Compare pins").click()
    page.get_by_role("menuitem", name="Compare red pins").click()

    expect(page).to_have_url(re.compile(r"/comparison/\d+$"))
    expect(page.locator(_MEMBERS)).to_have_count(2)
    expect(page.locator(_MEMBERS).filter(has_text="the sibling")).to_have_count(1)
    expect(page.locator(_MEMBERS).filter(has_text="Pin Then Compare")).to_have_count(1)
    assert console_errors == []


def test_selected_runs_start_a_comparison_that_keeps_the_experiments_charts(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _an_experiment_with_runs(page, live_server_url, "Compare Selected", 3)
    rows = _rows(page)
    rows.nth(0).locator(_CHECKBOX).check()
    rows.nth(1).locator(_CHECKBOX).check()

    page.locator("#run-list-bar-experiment").get_by_role("button", name="Compare", exact=True).click()
    page.get_by_role("menuitem", name="New comparison").click()

    expect(page).to_have_url(re.compile(r"/comparison/\d+$"))
    expect(page.locator(_MEMBERS)).to_have_count(2)
    # Its panels came with it: the same loss chart, drawing the two runs and not the third.
    expect(page.locator(".dl-panel-body .recharts-line-curve")).to_have_count(2)
    assert console_errors == []


def test_selected_runs_are_added_to_an_existing_comparison_and_it_is_shown(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    comparison = _a_shared_comparison_of_two_experiments("Add To Existing")
    store = get_system_data_store()
    (experiment,) = (e for e in store.get_experiments(comparison.project_id) if e.name == "baseline")
    extra = store.create_run(models.NewRun(experiment_id=experiment.id, name="extra-run"))
    page.goto(f"{live_server_url}/experiment/{experiment.id}")
    _rows(page).filter(has_text="extra-run").locator(_CHECKBOX).check()

    page.locator("#run-list-bar-experiment").get_by_role("button", name="Compare", exact=True).click()
    page.get_by_role("menuitem", name=re.compile("Sweep vs baseline")).click()

    expect(page).to_have_url(re.compile(rf"/comparison/{comparison.id}$"))
    expect(page.locator(_MEMBERS)).to_have_count(3)
    assert extra.id in {run.id for run in store.comparison_runs(comparison.id)}
    assert console_errors == []


def test_the_projects_pinned_runs_start_a_comparison_and_the_project_lists_comparisons(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    comparison = _a_shared_comparison_of_two_experiments("From The Project")
    store = get_system_data_store()
    me = store.get_or_create_user(models.Principal.unverified("pinner")).id
    for run in store.comparison_runs(comparison.id):
        store.pin_runs(me, frozenset({run.id}), models.PinColor.BLUE)
    page.goto(f"{live_server_url}/project/{comparison.project_id}")
    expect(page.locator(".comparison-card")).to_have_count(1)
    expect(page.locator(".comparison-card")).to_contain_text("Sweep vs baseline")

    rows = page.locator("#pinned-runs-grid .ag-center-cols-container .ag-row")
    expect(rows).to_have_count(2)
    rows.first.locator(_CHECKBOX).check()
    page.locator("#run-list-bar-project").get_by_role("button", name="Compare", exact=True).click()
    page.get_by_role("menuitem", name="New comparison").click()

    expect(page).to_have_url(re.compile(r"/comparison/\d+$"))
    expect(page.locator(_MEMBERS)).to_have_count(1)
    assert console_errors == []


def test_comparing_the_same_pinned_runs_again_opens_the_comparison_already_made(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    comparison = _a_shared_comparison_of_two_experiments("Compare Again Project")
    store = get_system_data_store()
    me = store.get_or_create_user(models.Principal.unverified(resolve_username())).id
    (first, *_) = store.comparison_runs(comparison.id)
    store.pin_runs(me, frozenset({first.id}), models.PinColor.RED)
    page.goto(f"{live_server_url}/project/{comparison.project_id}")
    rows = page.locator("#pinned-runs-grid .ag-center-cols-container .ag-row")
    expect(rows).to_have_count(1)
    rows.first.locator(".ag-selection-checkbox input").check()

    page.locator("#run-list-bar-project").get_by_role("button", name="Compare", exact=True).click()
    page.get_by_role("menuitem", name="New comparison").click()
    expect(page).to_have_url(re.compile(r"/comparison/\d+$"))
    opened = page.url

    page.goto(f"{live_server_url}/project/{comparison.project_id}")
    page.get_by_role("button", name="Compare pins").click()
    page.get_by_role("menuitem", name="Compare red pins").click()

    expect(page.get_by_text("Opened your comparison of these runs.")).to_be_visible()
    assert page.url == opened
    assert console_errors == []


def test_adding_pinned_runs_to_a_comparison_on_screen_changes_it_and_forks_nothing(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    comparison = _a_shared_comparison_of_two_experiments("Add Here Project")
    store = get_system_data_store()
    me = store.get_or_create_user(models.Principal.unverified(resolve_username())).id
    mine = store.create_comparison(
        BasicExperimentPage,
        models.NewPage[Any, Any](project_id=comparison.project_id, owner_id=me, name="mine", panels=[]),
        frozenset(comparison.run_ids[:1]),
    )
    (experiment,) = (e for e in store.get_experiments(comparison.project_id) if e.name == "sweep")
    extra = store.create_run(models.NewRun(experiment_id=experiment.id, name="pinned-extra"))
    store.pin_runs(me, frozenset({extra.id}), models.PinColor.RED)
    before = len(store.list_comparisons(comparison.project_id, me))
    page.goto(f"{live_server_url}/comparison/{mine.id}")
    rows = page.locator("#comparison-members-grid .ag-center-cols-container .ag-row")
    expect(rows).to_have_count(1)

    page.get_by_role("button", name="Compare pins").click()
    page.get_by_role("menuitem", name="Add red pins to this comparison").click()

    expect(rows).to_have_count(2)
    expect(page.get_by_text("Added 1 run.")).to_be_visible()
    assert page.url.endswith(f"/comparison/{mine.id}")
    assert len(store.list_comparisons(comparison.project_id, me)) == before
    assert {run.id for run in store.comparison_runs(mine.id)} == {comparison.run_ids[0], extra.id}
    assert console_errors == []
