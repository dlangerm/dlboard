"""Deleting and editing the charts a panel's chart filter matches, in a real browser (#509)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pendulum
import pytest
from playwright.sync_api import expect

from dlboard import models
from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.tests.browser_test import _auto_generate_charts, _create_project_and_experiment

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

pytestmark = pytest.mark.browser

_FAMILY_SIZE = 7
"""How many charts of each of the two families ("loss", "acc") the panel holds: 14 in all, more than a page."""


def _a_panel_of_two_families_of_charts(
    page: Page, live_server_url: str, name: str
) -> tuple[Locator, Locator]:
    """An experiment whose one panel ("g") holds a line chart of each of 7 `loss` and 7 `acc` metrics: its chart filter, and its charts."""
    _create_project_and_experiment(page, live_server_url, name)
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    letters = "abcdefg"[:_FAMILY_SIZE]
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=step,
                metrics={
                    f"g/{family}_{letter}": 1.0 + step for family in ("loss", "acc") for letter in letters
                },
                timestamp_utc=pendulum.now("UTC"),
            )
            for step in range(2)
        ]
    )
    page.reload()
    _auto_generate_charts(page)
    page.locator(".dl-panel-item-header", has_text="g").first.click()
    charts = page.locator(".dl-chart-item")
    expect(charts).to_have_count(12)
    return page.get_by_placeholder("Filter charts"), charts


def _panel_action(page: Page, name: str) -> None:
    """Choose `name` of the actions menu of the panel the page opened."""
    page.get_by_role("button", name="Panel actions").first.click()
    page.get_by_role("menuitem", name=name).click()


def test_the_charts_a_filter_matches_are_deleted_together_after_saying_how_many(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    chart_filter, charts = _a_panel_of_two_families_of_charts(page, live_server_url, "Bulk Delete Experiment")

    chart_filter.fill("loss")
    expect(charts).to_have_count(_FAMILY_SIZE)
    _panel_action(page, "Delete…")
    dialog = page.get_by_role("dialog", name="Delete these charts?")
    expect(dialog).to_contain_text(f"{_FAMILY_SIZE} charts")
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()
    expect(charts).to_have_count(_FAMILY_SIZE)  # asked, not done

    _panel_action(page, "Delete…")
    dialog.get_by_role("button", name="Delete").click()
    expect(dialog).to_be_hidden()

    # What the filter matched is gone, and only that: the other family is all the panel holds.
    expect(page.get_by_text(f"{_FAMILY_SIZE} charts")).to_be_visible()
    expect(charts).to_have_count(0)
    chart_filter.fill("")
    expect(charts).to_have_count(_FAMILY_SIZE)
    for chart in charts.all():
        expect(chart).to_contain_text("acc")
    assert console_errors == []


def test_the_x_axis_of_the_charts_a_filter_matches_is_set_together(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    chart_filter, charts = _a_panel_of_two_families_of_charts(page, live_server_url, "Bulk Edit Experiment")
    x_axes = page.locator("svg text", has_text="g/loss_a")

    chart_filter.fill("acc")
    expect(charts).to_have_count(_FAMILY_SIZE)
    _panel_action(page, "Set x-axis…")
    dialog = page.get_by_role("dialog", name="Set the x-axis of these charts")
    expect(dialog).to_contain_text(f"{_FAMILY_SIZE} charts")
    dialog.get_by_label("X-axis").fill("not-a-metric")
    dialog.get_by_role("button", name="Apply").click()
    expect(dialog).to_contain_text("Not a metric or hyperparameter")

    dialog.get_by_label("X-axis").fill("g/loss_a")
    dialog.get_by_role("button", name="Apply").click()
    expect(dialog).to_be_hidden()
    # Saved in a view of the viewer's own, as any edit of a page that is not theirs is.
    expect(page).to_have_url(re.compile(r"view="))
    expect(x_axes).to_have_count(_FAMILY_SIZE)

    # A chart the filter did not match is on the step it was. (Filtered by a key none of the edited charts shows: they now
    # read `g/loss_a` too, so a filter of "loss" finds them as well.)
    chart_filter.fill("loss_b")
    expect(charts).to_have_count(1)
    expect(charts.first).to_contain_text("g/loss_b")
    expect(x_axes).to_have_count(0)
    assert console_errors == []
    assert console_errors == []


def test_a_bulk_action_wants_a_chart_filter_since_without_one_it_would_reach_every_chart(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    chart_filter, charts = _a_panel_of_two_families_of_charts(
        page, live_server_url, "Bulk No Filter Experiment"
    )

    _panel_action(page, "Delete…")

    expect(page.get_by_text("Type in the panel's chart filter first")).to_be_visible()
    expect(page.get_by_role("dialog", name="Delete these charts?")).to_have_count(0)
    expect(charts).to_have_count(12)
    expect(chart_filter).to_have_value("")
    assert console_errors == []
