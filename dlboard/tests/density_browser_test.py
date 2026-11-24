"""A page is compact until its viewer chooses comfortable in the account menu, and stays as they chose."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

from dlboard.serve._density import Density

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser


def _main_padding(page: Page) -> float:
    return page.locator(".dl-app-main").evaluate("main => parseFloat(getComputedStyle(main).paddingRight)")


def test_a_page_is_compact_until_its_viewer_chooses_comfortable_and_stays_as_they_chose(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    account_menu = page.get_by_role("button", name=re.compile("^Account menu"))
    page.goto(live_server_url)
    expect(page.locator("html")).to_have_attribute("data-dl-density", Density.COMPACT)
    compact = _main_padding(page)

    account_menu.click()
    page.get_by_text(Density.COMFORTABLE.capitalize(), exact=True).click()

    expect(page.locator("html")).to_have_attribute("data-dl-density", Density.COMFORTABLE)
    assert _main_padding(page) > compact
    # Stamped before the page paints, not once Dash has drawn it.
    page.reload(wait_until="domcontentloaded")
    assert page.evaluate("document.documentElement.dataset.dlDensity") == Density.COMFORTABLE
    account_menu.click()
    expect(page.get_by_role("radio", name=Density.COMFORTABLE.capitalize())).to_be_checked()
    assert console_errors == []
