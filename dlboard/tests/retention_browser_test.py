"""Retention in a real browser: holding a project to a rule from its page, and cancelling the change."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

from dlboard.plugins.retention import _section as retention
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
    page.locator(f"#{retention.KIND_ID}").click()
    page.get_by_role("option", name="The newest runs").click()
    page.locator(f"#{retention.AMOUNT_ID}").fill("5")
    expect(page.locator(f"#{retention.PREVIEW_ID}")).to_contain_text("it would delete 0 runs")

    page.locator(f"#{retention.SAVE_ID}").click()
    pending = page.get_by_text(re.compile(r"Keeps the newest 5 runs of each experiment from"))
    expect(pending).to_be_visible()
    page.locator(f"#{retention.CANCEL_ID}").click()
    expect(pending).to_have_count(0)
    assert console_errors == []
