"""Retention in a real browser: holding a project to a rule from its page, and cancelling the change."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

from dlboard import models
from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.plugins.retention import _section as retention
from dlboard.serve._pages._experiment_picker import EXPERIMENT_PICKER_LIMIT
from dlboard.tests.browser_test import _create_project_and_experiment

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dash import Dash
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser


@pytest.fixture(autouse=True)
def _session_app_is_current(dlboard_app: Dash) -> Iterator[None]:
    with dlboard_app.server.app_context():
        yield


def test_a_project_is_held_to_a_rule_after_its_grace_period_and_the_change_can_be_cancelled(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _create_project_and_experiment(page, live_server_url, "Retention Rule Project")
    page.reload()
    page.get_by_role("tab", name="Retention").click()
    page.locator(f"#{retention.KIND_ID}").click()
    page.get_by_role("option", name="The newest runs").click()
    page.locator(f"#{retention.AMOUNT_ID}").fill("5")
    # Nothing is counted until it is asked for.
    expect(page.locator(f"#{retention.PREVIEW_ID}")).to_have_text("")
    page.locator(f"#{retention.PREVIEW_BUTTON_ID}").click()
    expect(page.locator(f"#{retention.PREVIEW_ID}")).to_contain_text("it would delete 0 runs")
    page.locator(f"#{retention.AMOUNT_ID}").fill("6")
    # A changed form is not what was counted.
    expect(page.locator(f"#{retention.PREVIEW_ID}")).to_have_text("")
    page.locator(f"#{retention.AMOUNT_ID}").fill("5")

    page.locator(f"#{retention.SAVE_ID}").click()
    pending = page.get_by_text(
        re.compile(
            r"From .+: Keeps the newest 5 runs of each experiment · deletes down to 5 once an experiment holds"
        )
    )
    expect(pending).to_be_visible()
    page.locator(f"#{retention.CANCEL_ID}").click()
    expect(pending).to_have_count(0)
    assert console_errors == []


def test_an_experiment_page_says_what_keeps_its_runs_and_links_to_where_that_is_set(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _create_project_and_experiment(page, live_server_url, "Retention Link Project")
    page.locator(".experiment-card", has_text="Retention Link Project").click()
    expect(page.get_by_text("Follows the project · Project: Keeps every run")).to_be_visible()

    page.get_by_role("link", name="Change →").click()

    # The project page opens on the tab the link names (#615).
    expect(page).to_have_url(re.compile(rf"/project/\d+\?tab={retention.RETENTION_TAB}$"))
    expect(page.get_by_role("tab", name="Retention")).to_have_attribute("aria-selected", "true")
    expect(page.locator(f"#{retention.SECTION_ID}")).to_be_visible()
    assert console_errors == []


def test_the_rule_for_picker_finds_an_experiment_past_the_newest_it_lists_by_searching_the_server(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """It lists the newest experiments, never every one; what someone types searches the server for the rest (#590)."""
    api = BasicDlboardAPI(live_server_url)
    project = api.create_project(models.NewProject(name="Retention Picker Project", description=""))
    api.create_experiment(models.NewExperiment(project_id=project.id, name="buried-experiment"))
    for index in range(EXPERIMENT_PICKER_LIMIT):
        api.create_experiment(models.NewExperiment(project_id=project.id, name=f"newer-{index}"))
    page.goto(f"{live_server_url}/project/{project.id}?tab={retention.RETENTION_TAB}")

    scope = page.locator(f"#{retention.SCOPE_ID}")
    scope.click()
    expect(page.get_by_role("option", name=f"newer-{EXPERIMENT_PICKER_LIMIT - 1}")).to_be_attached()
    expect(page.get_by_role("option", name="buried-experiment")).to_have_count(0)
    scope.fill("buried")
    page.get_by_role("option", name="buried-experiment").click()

    expect(scope).to_have_value("buried-experiment")
    assert console_errors == []
