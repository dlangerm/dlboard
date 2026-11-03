"""
The journeys of the run lists (#483) that cross pages, as a person takes them: from runs in one experiment to a comparison with
runs of another, and how a list of several experiments' runs reads in the sidebar's width.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pendulum
import pytest
from playwright.sync_api import expect

from dlboard import models
from dlboard.serve import get_system_data_store
from dlboard.serve._pages._experiment._page_model import BasicExperimentPage
from dlboard.serve._pages._experiment._runs_table import NAVBAR_HPARAM_DATATABLE_ID
from dlboard.tests.browser_test import _auto_generate_charts, _is_inside

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dash import Dash
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser

_LEGEND = ".mantine-ChartLegend-legendItemName"
_CHECKBOX = ".ag-selection-checkbox input"
_EXPERIMENT_ROWS = f"#{NAVBAR_HPARAM_DATATABLE_ID} .ag-center-cols-container .ag-row"
_COMPARISON_ROWS = "#comparison-members-grid .ag-center-cols-container .ag-row"


@pytest.fixture(autouse=True)
def _session_app_is_current(dlboard_app: Dash) -> Iterator[None]:
    """Read stores off `dlboard_app` in this module's helpers, as `browser_test.py` does."""
    with dlboard_app.server.app_context():
        yield


def _project_of_two_experiments(name: str) -> tuple[int, dict[str, int]]:
    """A project of "alpha" and "beta", each with two runs (named for their experiment) that logged a loss: its id, and theirs by name."""
    store = get_system_data_store()
    project = store.create_project(models.NewProject(name=name, description=""))
    experiments: dict[str, int] = {}
    for experiment_name in ("alpha", "beta"):
        experiment = store.create_experiment(
            models.NewExperiment(project_id=project.id, name=experiment_name)
        )
        experiments[experiment_name] = experiment.id
        for number in (1, 2):
            run = store.create_run(
                models.NewRun(
                    experiment_id=experiment.id,
                    name=f"{experiment_name}-{number}-with-a-name-too-long-for-the-list",
                )
            )
            store.log_metrics(
                models.LoggedMetrics(
                    metrics={"loss": 1.0 / (step + number)},
                    step=step,
                    experiment_id=experiment.id,
                    run_id=run.id,
                    timestamp_utc=pendulum.now("UTC"),
                )
                for step in range(3)
            )
    return project.id, experiments


def test_check_runs_of_one_experiment_compare_them_then_add_runs_of_another(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _, experiments = _project_of_two_experiments("Natural Compare Project")
    page.goto(f"{live_server_url}/experiment/{experiments['alpha']}")
    _auto_generate_charts(page)
    rows = page.locator(_EXPERIMENT_ROWS)
    expect(rows).to_have_count(2)
    rows.nth(0).locator(_CHECKBOX).check()
    rows.nth(1).locator(_CHECKBOX).check()

    # Without leaving the experiment: a comparison of the runs that are checked, drawn as the experiment's charts are.
    page.locator("#run-list-bar-experiment").get_by_role("button", name="Compare", exact=True).click()
    page.get_by_role("menuitem", name="New comparison").click()
    expect(page).to_have_url(re.compile(r"/comparison/\d+$"))
    comparison_url = page.url
    expect(page.locator(_COMPARISON_ROWS)).to_have_count(2)
    expect(page.locator(_LEGEND)).to_have_count(2)

    # Then from another experiment, one of its runs joins it.
    page.goto(f"{live_server_url}/experiment/{experiments['beta']}")
    _auto_generate_charts(page)
    page.locator(_EXPERIMENT_ROWS).filter(has_text="beta-1-").locator(_CHECKBOX).check()
    page.locator("#run-list-bar-experiment").get_by_role("button", name="Compare", exact=True).click()
    page.get_by_role("menu").get_by_role("menuitem").last.click()

    expect(page).to_have_url(comparison_url)
    expect(page.locator(_COMPARISON_ROWS)).to_have_count(3)
    # Whose each run is reads in the legend: its experiment, then its name.
    expect(page.locator(_LEGEND).filter(has_text="alpha / ")).to_have_count(2)
    expect(page.locator(_LEGEND).filter(has_text="beta / beta-1-")).to_have_count(1)
    assert console_errors == []


def test_a_list_of_runs_of_several_experiments_shows_which_experiment_each_is_of_in_the_sidebar(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    project_id, experiments = _project_of_two_experiments("Whose Run Project")
    store = get_system_data_store()
    user = store.get_or_create_user(models.Principal.unverified("pinner")).id
    runs = [
        run for experiment_id in experiments.values() for run in store.get_runs(experiment_id, run_ids=None)
    ]
    store.pin_runs(user, frozenset(run.id for run in runs), models.PinColor.RED)
    comparison = store.create_comparison(
        BasicExperimentPage,
        models.NewPage[Any, Any](project_id=project_id, owner_id=user, name="Both", shared=True, panels=[]),
        frozenset(run.id for run in runs),
    )

    for url, grid in (
        (f"/comparison/{comparison.id}", _COMPARISON_ROWS),
        (f"/project/{project_id}", "#pinned-runs-grid .ag-center-cols-container .ag-row"),
    ):
        page.goto(f"{live_server_url}{url}")
        rows = page.locator(grid)
        expect(rows).to_have_count(4)
        for row in rows.all():
            experiment = row.locator(".dl-run-experiment")
            # Under the run's name and drawn within the name's own cell, not pushed out of it by a name too long for the list.
            expect(experiment).to_have_text(re.compile("alpha|beta"))
            assert _is_inside(experiment, row.locator(".ag-cell", has=page.locator(".dl-run-experiment")))
            # ...and the name keeps room to be read (not collapsed to an ellipsis beside its dot).
            assert row.locator(".dl-run-link").evaluate("link => link.clientWidth > 50")
    assert console_errors == []
