"""
The run list in a real browser: the eye draws a run, the checkbox selects it, the bar acts on what is selected, and a link
to a run draws just that run.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

import pytest
from playwright.sync_api import expect

from dlboard import models
from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.models import RUN_NAME_COLUMN
from dlboard.serve import get_system_data_store
from dlboard.serve._pages._experiment._ids import METRIC_CONTENT_ID
from dlboard.serve._pages._experiment._live_updates import LIVE_POLL_INTERVAL_ID
from dlboard.serve._pages._experiment._runs_table import NAVBAR_HPARAM_DATATABLE_ID
from dlboard.serve._pages._run_list import RunListSurface
from dlboard.serve._pages._run_list._ids import bar_id
from dlboard.tests.browser_test import _auto_generate_charts, _create_project_and_experiment, _log_runs

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dash import Dash
    from playwright.sync_api import Locator, Page, Request

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


def _mark_the_panels(page: Page) -> Locator:
    """Mark the panel area, so a test can tell whether it was drawn again whole: the mark goes with the element."""
    panels = page.locator(f"#{METRIC_CONTENT_ID} > *").first
    panels.evaluate("el => el.setAttribute('data-marker', 'untouched')")
    return panels


def test_the_eye_draws_a_run_and_the_checkbox_only_selects_it(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """Drawing or hiding a run redraws its charts in place: the panels are never drawn again, nor covered (#616)."""
    _, _, rows = _experiment_of_runs(page, live_server_url, "Eye Experiment", 3)
    curves = page.locator(_CURVES)
    expect(curves).to_have_count(3)
    expect(_bar(page)).to_be_hidden()
    panels = _mark_the_panels(page)

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
    assert panels.get_attribute("data-marker") == "untouched"

    rows.first.locator(_CHECKBOX).uncheck()
    expect(_bar(page)).to_be_hidden()
    assert console_errors == []


def test_a_slow_redraw_of_a_toggled_run_leaves_the_charts_uncovered(
    page: Page, live_server_url: str, monkeypatch: pytest.MonkeyPatch, console_errors: list[str]
) -> None:
    """
    However long the charts take to redraw in place, the panel area's loader never covers them (#627).

    The redraw is slowed past the loader's delay, as a chart that reads artifacts slows it.
    """
    _, _, rows = _experiment_of_runs(page, live_server_url, "Slow Eye Experiment", 2)
    curves = page.locator(_CURVES)
    expect(curves).to_have_count(2)
    store_type = type(get_system_data_store())
    fetch_metrics = store_type.fetch_metrics

    def slow_fetch_metrics(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 -- whatever the store's does
        time.sleep(0.8)
        return fetch_metrics(*args, **kwargs)

    monkeypatch.setattr(store_type, "fetch_metrics", slow_fetch_metrics)
    page.evaluate(
        """(content) => {
            window.dlPanelLoaderSeen = false;
            const main = document.querySelector(".dl-experiment-main");
            const panels = document.getElementById(content);
            new MutationObserver(() => {
                for (const loader of main.querySelectorAll(".mantine-Loader-root")) {
                    window.dlPanelLoaderSeen ||= !panels.contains(loader);
                }
            }).observe(main, { childList: true, subtree: true, attributes: true });
        }""",
        str(METRIC_CONTENT_ID),
    )

    rows.first.locator(".dl-eye").click()

    expect(curves).to_have_count(1)
    assert page.evaluate("window.dlPanelLoaderSeen") is False
    assert console_errors == []


def test_drawing_or_hiding_a_run_costs_one_server_request(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    The draw or hide itself, and nothing after it: the run list it already wrote, the view it stayed in and the "Newest"
    button it drew again each cost a round trip that found nothing to do, and queued behind the next toggle.
    """
    _, _, rows = _experiment_of_runs(page, live_server_url, "One Request Experiment", 3)
    curves = page.locator(_CURVES)
    expect(curves).to_have_count(3)
    requests: list[str] = []

    def callback(request: Request) -> None:
        body: dict[str, Any] | None = (
            request.post_data_json if "_dash-update-component" in request.url else None
        )
        # The live poll's tick is the clock's, not the toggle's, as `page_load_budget_test.py` leaves it out too.
        if body is not None and all(i["id"] != LIVE_POLL_INTERVAL_ID for i in body.get("inputs", [])):
            requests.append(str(body.get("output")))

    page.on("request", callback)

    for drawn in (2, 3):
        requests.clear()
        rows.first.locator(".dl-eye").click()
        expect(curves).to_have_count(drawn)
        page.wait_for_load_state("networkidle")
        assert len(requests) == 1, requests
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


_HEADER = f'.ag-header-cell[col-id="{RUN_NAME_COLUMN}"]'


def _mark_the_grid(page: Page) -> Locator:
    """Mark the list's grid, so a test can tell whether it was drawn again: the mark goes with the element."""
    grid = page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID}")
    grid.locator(".ag-root-wrapper").evaluate("el => el.setAttribute('data-marker', 'untouched')")
    return grid


def test_a_run_that_starts_is_added_to_the_list_without_drawing_it_again(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """While training logs new runs the list keeps what was done in it: its sort, and the runs being selected (#513)."""
    experiment_id, runs, rows = _experiment_of_runs(page, live_server_url, "Run Starts Experiment", 3)
    grid = page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID}")
    grid.locator(_HEADER).click()
    expect(grid.locator(_HEADER)).to_have_attribute("aria-sort", "ascending")
    picked = runs[1].name or f"Run {runs[1].id}"
    rows.filter(has_text=picked).locator(_CHECKBOX).check()
    _mark_the_grid(page)

    started = _log_runs(BasicDlboardAPI(live_server_url), experiment_id, 1)[0]

    expect(rows).to_have_count(4, timeout=8000)
    expect(rows.filter(has_text=started.name or f"Run {started.id}")).to_have_count(1)
    expect(grid.locator(_HEADER)).to_have_attribute("aria-sort", "ascending")
    expect(rows.filter(has_text=picked).locator(_CHECKBOX)).to_be_checked()
    assert grid.locator(".ag-root-wrapper").get_attribute("data-marker") == "untouched"
    assert console_errors == []


def test_a_pin_made_by_someone_else_appears_in_the_list_without_drawing_it_again(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """A teammate's pin is found by the live poll, and patched in, with the selection being made left alone (#524)."""
    _, runs, rows = _experiment_of_runs(page, live_server_url, "Teammate Pins Experiment", 3)
    store = get_system_data_store()
    teammate = store.get_or_create_user(models.Principal.unverified("teammate")).id
    picked = runs[0].name or f"Run {runs[0].id}"
    pinned = runs[2].name or f"Run {runs[2].id}"
    rows.filter(has_text=picked).locator(_CHECKBOX).check()
    grid = _mark_the_grid(page)

    store.pin_runs(teammate, frozenset({runs[2].id}), models.PinColor.RED)

    expect(rows.filter(has_text=pinned).locator(".dl-pins .dl-icon-pin:not(.dl-pin-ghost)")).to_have_count(
        1, timeout=8000
    )
    expect(rows.filter(has_text=picked).locator(_CHECKBOX)).to_be_checked()
    assert grid.locator(".ag-root-wrapper").get_attribute("data-marker") == "untouched"

    store.unpin_runs(teammate, frozenset({runs[2].id}), models.PinColor.RED)

    expect(rows.filter(has_text=pinned).locator(".dl-pins .dl-icon-pin:not(.dl-pin-ghost)")).to_have_count(
        0, timeout=8000
    )
    assert grid.locator(".ag-root-wrapper").get_attribute("data-marker") == "untouched"
    assert console_errors == []


def test_a_teammates_first_pin_adds_the_menu_that_compares_the_pins_without_drawing_the_list_again(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """What a pin adds above the list is drawn in place too, and its buttons work."""
    _, runs, _rows = _experiment_of_runs(page, live_server_url, "Teammate Compare Experiment", 2)
    store = get_system_data_store()
    teammate = store.get_or_create_user(models.Principal.unverified("teammate")).id
    expect(page.get_by_role("button", name="Compare pins")).to_have_count(0)
    grid = _mark_the_grid(page)

    store.pin_runs(teammate, frozenset({runs[1].id}), models.PinColor.RED)

    compare = page.get_by_role("button", name="Compare pins")
    expect(compare).to_be_visible(timeout=8000)
    assert grid.locator(".ag-root-wrapper").get_attribute("data-marker") == "untouched"
    compare.click()
    page.get_by_role("menuitem", name="Compare red pins").click()
    expect(page).to_have_url(re.compile(r"/comparison/\d+$"))
    expect(page.locator("#comparison-members-grid .ag-center-cols-container .ag-row")).to_have_count(1)
    assert console_errors == []
