"""Pinning runs in a real browser: the pin on a run and its editor, the bar's Pin and Unpin, and the chips that find a pinned run."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

from dlboard import models
from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.serve import get_system_data_store
from dlboard.serve._pages._experiment._runs_table import NAVBAR_HPARAM_DATATABLE_ID
from dlboard.serve._pages._run_list import RunListSurface
from dlboard.serve._pages._run_list._ids import bar_id
from dlboard.tests.browser_test import _create_project_and_experiment, _log_runs

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dash import Dash
    from playwright.sync_api import Locator, Page

pytestmark = pytest.mark.browser

_PIN = ".dl-pins .dl-icon-pin:not(.dl-pin-ghost)"
"""The pin on a run: not the faint one an unpinned run shows when it is hovered."""
_COLORS = ("red", "orange", "yellow", "green", "teal", "blue", "violet", "pink", "gray")


@pytest.fixture(autouse=True)
def _session_app_is_current(dlboard_app: Dash) -> Iterator[None]:
    """Read stores off `dlboard_app` in this module's helpers, as `browser_test.py` does."""
    with dlboard_app.server.app_context():
        yield


def _experiment_of_runs(page: Page, live_server_url: str, name: str, count: int) -> tuple[int, Locator]:
    """An experiment of `count` runs, and the rows of its run table."""
    _create_project_and_experiment(page, live_server_url, name)
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    _log_runs(BasicDlboardAPI(live_server_url), experiment_id, count)
    page.reload()
    rows = page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID} .ag-center-cols-container .ag-row")
    # A long list draws only the rows in view: its size is what the grid says it has, and a header.
    expect(_grid(page)).to_have_attribute("aria-rowcount", str(count + 1))
    return experiment_id, rows


def _grid(page: Page) -> Locator:
    return page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID} .ag-root")


def _bar(page: Page) -> Locator:
    return page.locator(f"#{bar_id(RunListSurface.EXPERIMENT)}")


def test_a_run_is_pinned_in_several_colours_as_one_pin_and_unpinned_from_the_same_editor(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _, rows = _experiment_of_runs(page, live_server_url, "Pin Wheel Experiment", 2)
    grid = page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID}")
    grid.locator(".ag-root-wrapper").evaluate("el => el.setAttribute('data-marker', 'untouched')")
    pin = rows.first.locator(".dl-pins")
    editor = page.locator(".dl-pin-editor")

    pin.click()
    editor.get_by_role("button", name="Pin red").click()
    expect(rows.first.locator(_PIN)).to_have_count(1)
    expect(rows.first.locator(_PIN)).to_have_css("background-color", re.compile(r"rgb"))
    editor.get_by_role("button", name="Pin blue").click()
    # Two colours are one pin filled with both, not two pins.
    expect(rows.first.locator(_PIN)).to_have_count(1)
    expect(rows.first.locator(_PIN)).to_have_css("background-image", re.compile(r"conic-gradient"))
    expect(editor.get_by_role("button", name="Pin red")).to_have_attribute("aria-pressed", "true")

    # Taking one back leaves the other; the editor says what is yours.
    editor.get_by_role("button", name="Pin red").click()
    expect(editor.get_by_role("button", name="Pin red")).to_have_attribute("aria-pressed", "false")
    expect(rows.first.locator(_PIN)).not_to_have_css("background-image", re.compile(r"conic-gradient"))
    expect(rows.nth(1).locator(_PIN)).to_have_count(0)

    # No colour is named anywhere on the page, and the table was patched in place, never drawn again.
    page.keyboard.press("Escape")
    expect(editor).to_have_count(0)
    # In what the run list shows, which is all this is about: other tests of the session leave their own names on the page.
    shown = grid.inner_text().lower()
    assert not [color for color in _COLORS if re.search(rf"\b{color}\b", shown)]
    assert grid.locator(".ag-root-wrapper").get_attribute("data-marker") == "untouched"
    page.reload()
    expect(rows.first.locator(_PIN)).to_have_count(1)
    assert console_errors == []


def test_the_bar_pins_and_unpins_every_selected_run(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _, rows = _experiment_of_runs(page, live_server_url, "Bar Pins Experiment", 3)
    rows.nth(0).locator(".ag-selection-checkbox input").check()
    rows.nth(1).locator(".ag-selection-checkbox input").check()

    _bar(page).get_by_role("button", name="Pin", exact=True).click()
    page.get_by_role("menuitem", name="Pin green").click()
    expect(page.get_by_text("Pinned 2 runs")).to_be_visible()
    expect(rows.nth(0).locator(_PIN)).to_have_count(1)
    expect(rows.nth(1).locator(_PIN)).to_have_count(1)
    expect(rows.nth(2).locator(_PIN)).to_have_count(0)

    _bar(page).get_by_role("button", name="Unpin", exact=True).click()
    page.get_by_role("menuitem", name="Unpin green").click()
    expect(rows.nth(0).locator(_PIN)).to_have_count(0)
    expect(rows.nth(1).locator(_PIN)).to_have_count(0)
    # The runs are still selected: the bar acts on them again, as it does after any action.
    expect(_bar(page).get_by_text("2 selected")).to_be_visible()
    assert console_errors == []


def test_a_pinned_run_far_down_a_long_list_is_found_by_its_pin_chip(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    experiment_id, rows = _experiment_of_runs(page, live_server_url, "Pin Chips Experiment", 30)
    store = get_system_data_store()
    oldest = next(iter(reversed(list(store.get_runs(experiment_id, limit=None, run_ids=None)))))
    newest = next(iter(store.get_runs(experiment_id, limit=1, run_ids=None)))
    user = store.get_or_create_user(models.Principal.unverified("anonymous")).id
    store.pin_runs(user, frozenset({oldest.id}), models.PinColor.RED)
    store.pin_runs(user, frozenset({newest.id}), models.PinColor.GREEN)
    page.reload()
    expect(rows.first).to_be_visible()

    chips = page.get_by_role("checkbox", name=re.compile("Only .* pins"))
    expect(chips).to_have_count(2)  # a chip for each colour some run has, and no other
    # A chip is a checkbox hidden behind its label, which is what is clicked.
    red = page.get_by_role("checkbox", name="Only red pins").locator("xpath=following-sibling::label")
    green = page.get_by_role("checkbox", name="Only green pins").locator("xpath=following-sibling::label")
    red.click()
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text(oldest.name or f"Run {oldest.id}")
    green.click()
    expect(rows).to_have_count(2)
    red.click()
    green.click()
    expect(_grid(page)).to_have_attribute("aria-rowcount", "31")
    assert console_errors == []


def test_a_pin_names_who_pinned_the_run_in_its_tooltip(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    experiment_id, rows = _experiment_of_runs(page, live_server_url, "Pin Tooltip Experiment", 1)
    store = get_system_data_store()
    (run,) = store.get_runs(experiment_id, limit=1, run_ids=None)
    bob = store.get_or_create_user(models.Principal.unverified("bob")).id
    store.pin_runs(bob, frozenset({run.id}), models.PinColor.TEAL)
    page.reload()

    rows.first.locator(".dl-pins").hover()

    expect(page.locator(".dl-pins-tooltip")).to_contain_text("bob")
    assert console_errors == []


def _project_with_pinned_runs(
    page: Page, live_server_url: str, name: str
) -> tuple[int, dict[str, models.Run]]:
    """A project of two experiments with two pinned runs, `alpha` in four pins and `gamma` in one: its id, and the runs."""
    _create_project_and_experiment(page, live_server_url, name)
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    store = get_system_data_store()
    project_id = store.get_experiment(experiment_id).project_id  # pyrefly: ignore [missing-attribute]
    other = store.create_experiment(models.NewExperiment(project_id=project_id, name="the other one"))
    runs = {
        "alpha": store.create_run(models.NewRun(experiment_id=experiment_id, name="alpha")),
        "gamma": store.create_run(models.NewRun(experiment_id=other.id, name="gamma")),
    }
    alice, bob = (store.get_or_create_user(models.Principal.unverified(n)).id for n in ("alice", "bob"))
    for user, color in (
        (alice, models.PinColor.RED),
        (alice, models.PinColor.BLUE),
        (bob, models.PinColor.RED),
    ):
        store.pin_runs(user, frozenset({runs["alpha"].id}), color)
    store.pin_runs(bob, frozenset({runs["alpha"].id}), models.PinColor.GREEN)
    store.pin_runs(bob, frozenset({runs["gamma"].id}), models.PinColor.PLAIN)
    page.goto(f"{live_server_url}/project/{project_id}")
    return project_id, runs


def _pinned(page: Page) -> tuple[Locator, Locator]:
    """The grid of the project page's pinned runs, and its rows."""
    grid = page.locator("#pinned-runs-grid")
    return grid.locator(".ag-root"), grid.locator(".ag-center-cols-container .ag-row")


def test_the_project_page_lists_a_pinned_run_once_however_many_pins_it_has(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _project_with_pinned_runs(page, live_server_url, "Pinned Once Project")
    grid, rows = _pinned(page)

    expect(grid).to_have_attribute("aria-rowcount", "3")  # two runs, and a header: not five pins
    expect(page.locator("#pinned-runs-count")).to_have_text("2 runs")
    alpha = rows.filter(has_text="alpha")
    expect(alpha.locator(_PIN)).to_have_count(1)
    expect(alpha).to_contain_text("Pinned Once Project")  # the experiment it is of
    assert console_errors == []


def test_a_pin_is_taken_back_from_the_project_page(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _project_with_pinned_runs(page, live_server_url, "Unpin From Project")
    _, rows = _pinned(page)
    gamma = rows.filter(has_text="gamma")
    editor = page.locator(".dl-pin-editor")

    gamma.locator(".dl-pins").click()
    editor.get_by_role("button", name="Pin green").click()
    expect(editor.get_by_role("button", name="Pin green")).to_have_attribute("aria-pressed", "true")
    page.reload()
    gamma.locator(".dl-pins").click()
    expect(editor.get_by_role("button", name="Pin green")).to_have_attribute("aria-pressed", "true")
    editor.get_by_role("button", name="Pin green").click()
    expect(editor.get_by_role("button", name="Pin green")).to_have_attribute("aria-pressed", "false")
    # The pin someone else made is not yours to take back.
    expect(editor.get_by_role("button", name="Pin plain")).to_have_attribute("aria-pressed", "false")
    assert console_errors == []


def test_filtering_the_pinned_runs_by_colour_is_in_the_address(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    project_id, _ = _project_with_pinned_runs(page, live_server_url, "Filtered Pins Project")
    grid, rows = _pinned(page)
    expect(grid).to_have_attribute("aria-rowcount", "3")

    page.get_by_role("checkbox", name="Only plain pins").locator("xpath=following-sibling::label").click()

    expect(grid).to_have_attribute("aria-rowcount", "2")
    expect(rows.first).to_contain_text("gamma")
    expect(page).to_have_url(re.compile(r"pins_color=plain"))
    page.goto(page.url)
    expect(grid).to_have_attribute("aria-rowcount", "2")
    expect(page.get_by_role("checkbox", name="Only plain pins")).to_be_checked()
    assert page.url.endswith(f"/project/{project_id}?pins_color=plain")
    assert console_errors == []


def test_a_pinned_runs_name_goes_to_the_run_in_its_experiment(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _, runs = _project_with_pinned_runs(page, live_server_url, "Go To Run Project")
    _, rows = _pinned(page)

    rows.filter(has_text="gamma").get_by_role("link", name="gamma").click()

    expect(page).to_have_url(
        re.compile(rf"/experiment/{runs['gamma'].experiment_id}\?run={runs['gamma'].id}")
    )
    expect(page.locator("#focus-banner")).to_contain_text("gamma")
    assert console_errors == []


def test_the_selected_pinned_runs_are_deleted_from_the_project_page(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _, runs = _project_with_pinned_runs(page, live_server_url, "Delete Pinned Project")
    _, rows = _pinned(page)
    rows.filter(has_text="gamma").locator(".ag-selection-checkbox input").check()

    page.locator("#run-list-bar-project").get_by_role("button", name="Delete").click()
    page.get_by_role("dialog", name="Delete these runs?").get_by_role("button", name="Delete").click()

    expect(page.get_by_text("alpha")).to_be_visible()
    expect(rows).to_have_count(1)
    assert {run.id for run in get_system_data_store().list_deleted_runs()} >= {runs["gamma"].id}
    assert runs["alpha"].id not in {run.id for run in get_system_data_store().list_deleted_runs()}
    assert console_errors == []


def test_unpinning_takes_a_run_out_of_a_colour_filter_it_no_longer_matches(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _, rows = _experiment_of_runs(page, live_server_url, "Unpin Filter Experiment", 2)
    rows.nth(0).locator(".ag-selection-checkbox input").check()
    rows.nth(1).locator(".ag-selection-checkbox input").check()
    _bar(page).get_by_role("button", name="Pin", exact=True).click()
    page.get_by_role("menuitem", name="Pin red").click()
    expect(page.get_by_text("Pinned 2 runs")).to_be_visible()
    page.reload()
    page.get_by_role("checkbox", name="Only red pins").locator("xpath=following-sibling::label").click()
    expect(rows).to_have_count(2)

    rows.nth(1).locator(".ag-selection-checkbox input").uncheck()
    rows.nth(0).locator(".ag-selection-checkbox input").uncheck()
    rows.nth(1).locator(".ag-selection-checkbox input").check()
    _bar(page).get_by_role("button", name="Unpin", exact=True).click()
    page.get_by_role("menuitem", name="Unpin red").click()

    expect(rows).to_have_count(1)
    assert console_errors == []
