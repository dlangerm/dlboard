"""A project page in a real browser: its experiment cards a page at a time, a filter by name, and its tab, all in its link (#502, #615)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

from dlboard import models
from dlboard.serve import get_system_data_store
from dlboard.serve._pages._card_grid import PAGE_SIZE, CardGrid

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dash import Dash
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser

_EXPERIMENTS = CardGrid.EXPERIMENTS


@pytest.fixture(autouse=True)
def _session_app_is_current(dlboard_app: Dash) -> Iterator[None]:
    with dlboard_app.server.app_context():
        yield


def test_a_project_shows_its_newest_experiments_a_page_at_a_time_and_its_link_keeps_the_page_and_filter(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    store = get_system_data_store()
    project = store.create_project(models.NewProject(name="Many Experiments Project", description=""))
    for number in range(PAGE_SIZE + 6):
        store.create_experiment(models.NewExperiment(project_id=project.id, name=f"sweep-{number:02}"))
    cards = page.locator(".experiment-card")

    page.goto(f"{live_server_url}/project/{project.id}")
    expect(cards).to_have_count(PAGE_SIZE)
    expect(cards.first).to_contain_text("sweep-29")  # the newest first
    expect(page.locator(f"#{_EXPERIMENTS.count_id}")).to_contain_text(f"Experiments · {PAGE_SIZE + 6}")

    page.locator(f"#{_EXPERIMENTS.pager_id}").get_by_role("button", name="2").click()
    expect(cards).to_have_count(6)
    expect(cards.last).to_contain_text("sweep-00")
    expect(page).to_have_url(re.compile(r"exps_page=2"))

    page.get_by_role("textbox", name="Filter experiments").fill("sweep-1")
    expect(cards).to_have_count(10)  # sweep-10 .. sweep-19, from the first page again
    expect(page).to_have_url(re.compile(r"exps_q=sweep-1(?!.*exps_page)"))

    page.reload()  # the link shows what was seen
    expect(cards).to_have_count(10)
    expect(page.get_by_role("textbox", name="Filter experiments")).to_have_value("sweep-1")
    assert console_errors == []


def test_a_project_page_keeps_its_tab_in_its_link_and_opens_on_its_experiments_otherwise(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """Comparisons, sharing and what plugins add are each a tab of the page, not more of it to scroll past (#615)."""
    project = get_system_data_store().create_project(models.NewProject(name="Tabbed Project", description=""))
    tab = page.get_by_role("tab", name="Comparisons")

    page.goto(f"{live_server_url}/project/{project.id}?tab=nonsense")
    expect(page.get_by_role("tab", name="Experiments")).to_have_attribute("aria-selected", "true")
    expect(page.get_by_text("No experiments yet")).to_be_visible()

    tab.click()
    expect(page.get_by_text("No comparisons yet")).to_be_visible()
    expect(page.get_by_text("No experiments yet")).to_be_hidden()
    expect(page).to_have_url(re.compile(r"[?&]tab=comparisons(&|$)"))

    page.reload()  # the link opens the tab that was open
    expect(tab).to_have_attribute("aria-selected", "true")
    expect(page.get_by_text("No comparisons yet")).to_be_visible()

    # An experiment made from the header, whatever tab is open, is shown where it is listed.
    page.get_by_role("textbox", name="New experiment name").fill("from-another-tab")
    page.get_by_role("button", name="Create").click()
    expect(page.get_by_role("tab", name="Experiments")).to_have_attribute("aria-selected", "true")
    expect(page.locator(".experiment-card", has_text="from-another-tab")).to_be_visible()
    assert console_errors == []
