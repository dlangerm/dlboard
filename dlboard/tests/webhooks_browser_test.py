"""Webhooks in a real browser: an owner adds one from the project page, sees its secret once, and deletes it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

from dlboard.plugins.webhooks import _tab as webhooks
from dlboard.tests.browser_test import _create_project_and_experiment

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dash import Dash
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser

_URL = "https://93.184.215.14/hook"
"""A public address, given as one: checking it needs no DNS."""


@pytest.fixture(autouse=True)
def _session_app_is_current(dlboard_app: Dash) -> Iterator[None]:
    with dlboard_app.server.app_context():
        yield


def test_an_owner_adds_a_webhook_sees_its_secret_once_and_deletes_it(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _create_project_and_experiment(page, live_server_url, "Webhooks Project")
    page.reload()
    page.get_by_role("tab", name="Webhooks").click()
    page.locator(f"#{webhooks.URL_ID}").fill(_URL)
    page.locator(f"#{webhooks.ADD_ID}").click()

    notice = page.locator(f"#{webhooks.NOTICE_ID}")
    expect(notice).to_contain_text("it won't be shown again")
    secret = notice.locator("pre, code").first.inner_text().strip()
    assert len(secret) >= 32
    section = page.locator(f"#{webhooks.SECTION_ID}")
    expect(section.get_by_text(_URL)).to_be_visible()

    page.reload()
    page.get_by_role("tab", name="Webhooks").click()
    expect(section.get_by_text(_URL)).to_be_visible()
    assert secret not in page.content()

    section.get_by_role("button", name="Delete").click()
    expect(section.get_by_text("No webhooks yet.")).to_be_visible()
    assert console_errors == []
