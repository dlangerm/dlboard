"""
The run list in a real browser: the eye draws a run, the checkbox selects it, the bar acts on what is selected, and a link
to a run draws just that run.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

from dlboard import models
from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.models import RUN_NAME_COLUMN
from dlboard.serve import get_system_data_store
from dlboard.serve._pages._experiment._runs_table import NAVBAR_HPARAM_DATATABLE_ID
from dlboard.serve._pages._run_list import RunListSurface
from dlboard.serve._pages._run_list._ids import bar_id
from dlboard.tests.browser_test import _auto_generate_charts, _create_project_and_experiment, _log_runs

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dash import Dash
    from playwright.sync_api import Locator, Page

pytestmark = pytest.mark.browser

_CURVES = ".dl-panel-body .recharts-line-curve"
_CHECKBOX = ".ag-selection-checkbox input"


@pytest.fixture(autouse=True)
def _session_app_is_current(dlboard_app: Dash) -> Iterator[None]:
    """Read stores off `dlboard_app` in this module's helpers, as `browser_test.py` does."""
    with dlboard_app.server.app_context():
        yield


def _experiment_of_runs(
    page: Page, live_server_url: str, name: str, count: int
) -> tuple[int, list[models.Run], Locator]:
    """An experiment of `count` runs with its charts drawn: its id, the runs oldest first, and the rows of its run table."""
    _create_project_and_experiment(page, live_server_url, name)
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    runs = _log_runs(BasicDlboardAPI(live_server_url), experiment_id, count)
    page.reload()
    _auto_generate_charts(page)
    rows = page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID} .ag-center-cols-container .ag-row")
    expect(rows).to_have_count(count)
    return experiment_id, runs, rows


def _bar(page: Page) -> Locator:
    return page.locator(f"#{bar_id(RunListSurface.EXPERIMENT)}")


def test_the_eye_draws_a_run_and_the_checkbox_only_selects_it(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _, _, rows = _experiment_of_runs(page, live_server_url, "Eye Experiment", 3)
    curves = page.locator(_CURVES)
    expect(curves).to_have_count(3)
    expect(_bar(page)).to_be_hidden()

    # Selecting a run draws nothing and hides nothing: it offers the actions on it.
    rows.first.locator(_CHECKBOX).check()
    expect(_bar(page)).to_be_visible()
    expect(_bar(page).get_by_text("1 selected")).to_be_visible()
    expect(curves).to_have_count(3)

    # The bar acts on what is selected, and what it hid is still selected for the next thing.
    _bar(page).get_by_role("button", name="Hide").click()
    expect(curves).to_have_count(2)
    expect(rows.first.locator(".dl-eye")).to_have_class(re.compile(r"dl-icon-eye-off"))
    expect(rows.first.locator(_CHECKBOX)).to_be_checked()
    _bar(page).get_by_role("button", name="Show").click()
    expect(curves).to_have_count(3)

    # The eye is the run's own: one click, nothing to select first.
    rows.nth(1).locator(".dl-eye").click()
    expect(curves).to_have_count(2)
    rows.nth(1).locator(".dl-eye").click()
    expect(curves).to_have_count(3)

    rows.first.locator(_CHECKBOX).uncheck()
    expect(_bar(page)).to_be_hidden()
    assert console_errors == []


def test_deleting_the_selected_runs_asks_once_and_deletes_them_all(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    experiment_id, runs, rows = _experiment_of_runs(page, live_server_url, "Delete Runs Experiment", 3)
    rows.nth(0).locator(_CHECKBOX).check()
    rows.nth(1).locator(_CHECKBOX).check()

    _bar(page).get_by_role("button", name="Delete").click()
    dialog = page.get_by_role("dialog", name="Delete these runs?")
    expect(dialog).to_contain_text("these 2 runs")
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()
    expect(rows).to_have_count(3)

    _bar(page).get_by_role("button", name="Delete").click()
    dialog.get_by_role("button", name="Delete").click()

    expect(rows).to_have_count(1)
    deleted = {run.id for run in get_system_data_store().list_deleted_runs()}
    assert {runs[1].id, runs[2].id} <= deleted
    assert runs[0].id not in deleted
    assert experiment_id == runs[0].experiment_id
    assert console_errors == []


def test_a_diff_of_more_runs_than_it_can_read_side_by_side_is_refused(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _, _, rows = _experiment_of_runs(page, live_server_url, "Diff Too Many Experiment", 6)
    for index in range(6):
        rows.nth(index).locator(_CHECKBOX).check()

    _bar(page).get_by_role("button", name="Diff").click()

    expect(page.get_by_text("Too many runs to diff")).to_be_visible()
    expect(page.get_by_role("dialog", name="Compare runs")).to_have_count(0)
    assert console_errors == []


def test_a_link_to_a_run_draws_just_that_run_and_says_so(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _, runs, rows = _experiment_of_runs(page, live_server_url, "Focus Experiment", 3)
    curves = page.locator(_CURVES)
    expect(curves).to_have_count(3)
    oldest = runs[0].name or f"Run {runs[0].id}"

    rows.filter(has_text=oldest).get_by_role("link", name=oldest).click()

    expect(page).to_have_url(re.compile(rf"run={runs[0].id}"))
    expect(page.locator("#focus-banner")).to_contain_text(oldest)
    expect(curves).to_have_count(1)
    expect(rows.filter(has_text=oldest)).to_have_class(re.compile(r"dl-run-focused"))
    expect(rows.filter(has_text=oldest).locator(".dl-eye")).to_have_class(re.compile(r"dl-icon-eye\b"))

    # Leaving it is a link, and the view's own runs are drawn again, as it was saved.
    page.get_by_role("link", name="Show the view's runs").click()
    expect(page.locator("#focus-banner")).to_have_count(0)
    expect(curves).to_have_count(3)
    assert console_errors == []


def test_drawing_another_run_while_looking_at_one_leaves_the_link(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _, runs, rows = _experiment_of_runs(page, live_server_url, "Leave Focus Experiment", 3)
    newest = runs[-1].name or f"Run {runs[-1].id}"
    curves = page.locator(_CURVES)
    rows.filter(has_text=newest).get_by_role("link", name=newest).click()
    expect(curves).to_have_count(1)

    rows.nth(2).locator(".dl-eye").click()

    expect(page.locator("#focus-banner")).to_have_count(0)
    expect(page).not_to_have_url(re.compile(r"run="))
    expect(curves).to_have_count(3)
    assert console_errors == []


def test_a_selection_in_progress_survives_the_table_being_drawn_again(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    experiment_id, runs, rows = _experiment_of_runs(page, live_server_url, "Selection Kept Experiment", 2)
    picked = runs[0].name or f"Run {runs[0].id}"
    rows.filter(has_text=picked).locator(_CHECKBOX).check()

    # A run starts while someone is choosing: the table is drawn again with it in, and the choice is still made.
    _log_runs(BasicDlboardAPI(live_server_url), experiment_id, 1)
    expect(rows).to_have_count(3, timeout=8000)

    expect(rows.filter(has_text=picked).locator(_CHECKBOX)).to_be_checked()
    expect(_bar(page).get_by_text("1 selected")).to_be_visible()
    assert console_errors == []


def test_the_breadcrumb_switches_to_another_experiment_of_the_project(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _create_project_and_experiment(page, live_server_url, "Switch Project")
    page.locator(".experiment-card").click()
    first_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    store = get_system_data_store()
    project_id = store.get_experiment(first_id).project_id  # pyrefly: ignore [missing-attribute]
    second = store.create_experiment(models.NewExperiment(project_id=project_id, name="the other one"))
    page.reload()

    page.get_by_role("button", name=re.compile("Switch experiment")).click()
    page.get_by_role("menuitem", name="the other one").click()

    expect(page).to_have_url(re.compile(rf"/experiment/{second.id}$"))
    assert console_errors == []


def test_drawing_a_run_keeps_the_filter_the_list_is_narrowed_by(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """Drawing a run patches its row; it does not draw the list again, which would clear the column filter and scroll."""
    _, runs, rows = _experiment_of_runs(page, live_server_url, "Keep Filter Experiment", 6)
    grid = page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID}")
    target = runs[2].name or f"Run {runs[2].id}"
    grid.locator(f'.ag-header-cell[col-id="{RUN_NAME_COLUMN}"]').hover()
    grid.locator(f'.ag-header-cell[col-id="{RUN_NAME_COLUMN}"] .ag-header-cell-filter-button').click()
    page.locator(".ag-filter-body input").first.fill(target)
    expect(rows).to_have_count(1)
    page.keyboard.press("Escape")

    rows.first.locator(".dl-eye").click()

    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text(target)
    expect(rows.first.locator(".dl-eye")).to_have_class(re.compile(r"dl-icon-eye-off"))
    assert console_errors == []
