"""Retention in a real browser: keeping a run from its list, and holding a project to a rule from its page."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.serve._pages import _project_retention as retention
from dlboard.serve._pages._experiment._runs_table import NAVBAR_HPARAM_DATATABLE_ID
from dlboard.serve._pages._run_list import RunListSurface
from dlboard.serve._pages._run_list._ids import bar_id
from dlboard.tests.browser_test import _create_project_and_experiment, _log_runs

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dash import Dash
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser

_LOCK = ".dl-run-kept"


@pytest.fixture(autouse=True)
def _session_app_is_current(dlboard_app: Dash) -> Iterator[None]:
    with dlboard_app.server.app_context():
        yield


def test_the_bar_keeps_the_selected_runs_and_releases_them(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _create_project_and_experiment(page, live_server_url, "Kept Runs Experiment")
    page.locator(".experiment-card").filter(has_text="Kept Runs Experiment").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    _log_runs(BasicDlboardAPI(live_server_url), experiment_id, 2)
    page.reload()
    rows = page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID} .ag-center-cols-container .ag-row")
    expect(rows).to_have_count(2)
    bar = page.locator(f"#{bar_id(RunListSurface.EXPERIMENT)}")

    rows.nth(0).locator(".ag-selection-checkbox input").check()
    bar.get_by_role("button", name="Keep", exact=True).click()
    expect(page.get_by_text("Kept 1 run")).to_be_visible()
    expect(page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID} {_LOCK}")).to_have_count(1)

    bar.get_by_role("button", name="Release", exact=True).click()
    expect(page.get_by_text("Released 1 run")).to_be_visible()
    expect(page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID} {_LOCK}")).to_have_count(0)
    assert console_errors == []


def test_a_project_is_held_to_a_rule_after_its_grace_period_and_the_change_can_be_cancelled(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _create_project_and_experiment(page, live_server_url, "Retention Rule Project")
    page.reload()
    page.locator(f"#{retention.KIND_ID}").click()
    page.get_by_role("option", name="The newest runs").click()
    page.locator(f"#{retention.AMOUNT_ID}").fill("5")
    expect(page.locator(f"#{retention.PREVIEW_ID}")).to_contain_text("it would delete 0 runs")

    page.locator(f"#{retention.SAVE_ID}").click()
    pending = page.get_by_text(re.compile(r"Keeps the newest 5 runs of each experiment from"))
    expect(pending).to_be_visible()
    page.locator(f"#{retention.CANCEL_ID}").click()
    expect(pending).to_have_count(0)

    page.goto(f"{live_server_url}/admin?tab=retention")
    expect(page.get_by_role("cell", name="Retention Rule Project", exact=True)).to_be_visible()
    assert console_errors == []
