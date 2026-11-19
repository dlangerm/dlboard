"""Tagging runs in a real browser: the bar's tag box tags the selected runs and untags them, and the tags follow their names."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.models import RUN_NAME_COLUMN
from dlboard.serve._pages._experiment._runs_table import NAVBAR_HPARAM_DATATABLE_ID
from dlboard.serve._pages._run_list import RunListSurface
from dlboard.serve._pages._run_list._ids import bar_id
from dlboard.tests.browser_test import _create_project_and_experiment, _log_runs

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dash import Dash
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser


@pytest.fixture(autouse=True)
def _session_app_is_current(dlboard_app: Dash) -> Iterator[None]:
    with dlboard_app.server.app_context():
        yield


def test_the_bar_tags_the_selected_runs_and_untags_them(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _create_project_and_experiment(page, live_server_url, "Tagged Runs Experiment")
    page.locator(".experiment-card").filter(has_text="Tagged Runs Experiment").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    _log_runs(BasicDlboardAPI(live_server_url), experiment_id, 2)
    page.reload()
    rows = page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID} .ag-center-cols-container .ag-row")
    expect(rows).to_have_count(2)
    bar = page.locator(f"#{bar_id(RunListSurface.EXPERIMENT)}")
    tagged = page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID} .dl-run-tag", has_text="baseline")

    rows.nth(0).locator(".ag-selection-checkbox input").check()
    bar.get_by_placeholder("tag").fill(" Baseline")
    bar.get_by_role("button", name="Tag", exact=True).click()
    expect(page.get_by_text("Tagged 1 run 'baseline'")).to_be_visible()
    expect(tagged).to_have_count(1)

    # The Run column's filter finds a run by a tag of it as well as by its name.
    name_header = page.locator(f'#{NAVBAR_HPARAM_DATATABLE_ID} .ag-header-cell[col-id="{RUN_NAME_COLUMN}"]')
    name_header.hover()
    name_header.locator(".ag-header-cell-filter-button").click()
    page.locator(".ag-filter-body input").first.fill("baseline")
    expect(rows).to_have_count(1)
    page.keyboard.press("Escape")

    bar.get_by_role("button", name="Untag", exact=True).click()
    expect(page.get_by_text("Took 'baseline' off 1 run")).to_be_visible()
    expect(tagged).to_have_count(0)

    # The project keeps the tag, and the box offers it to whoever types part of it.
    bar.get_by_placeholder("tag").fill("BASE")
    expect(page.get_by_role("option", name="baseline")).to_be_visible()
    assert console_errors == []
