"""
Browser-driven end-to-end regression tests for the composed dlboard app.

Exercises the app the way a real deployment is wired -- `dlboard.serve.app.app()` with the same
plugin bundle `dlboard serve local` uses -- through an actual rendered browser (Playwright). A real browser
is expensive to spin up, so this file is deliberately kept to a couple of full flows rather than
many shallow ones, and each flow is chosen to cover something a Python-level unit test structurally
can't: clientside (pure-JS, no Python round-trip) callbacks, the actual chart JS library rendering
real SVG from real data, and multi-page navigation continuity -- not "does this container become
visible", which `admin_page_test.py` and friends already prove at the Python level far more
cheaply.

Runs the app itself with a plain `werkzeug` threaded server rather than `dash[testing]`'s
`dash_duo` fixture: that fixture drags in a `selenium<=4.2.0` pin (predates Selenium Manager,
launches Chrome with a `--headless` flag modern Chrome has dropped support for) that broke against
both a current local Chrome and CI's. Playwright manages its own bundled, version-matched browser
instead, sidestepping that whole class of problem.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlparse

import numpy as np
import pendulum
import pytest
from playwright.sync_api import expect

from dlboard import models
from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.client.artifacts import rle
from dlboard.client.artifacts.image import Image
from dlboard.client.artifacts.overlay import DEFAULT_CLASS_COLORS, Box, Overlay, Point, Polygon, Shape
from dlboard.client.histograms import ExponentialBinning
from dlboard.models import OVERLAY_KEY_SUFFIX, PanelInstance, overlay_key
from dlboard.serve import get_system_data_store
from dlboard.serve._jump import JUMP_SELECT_ID
from dlboard.serve._pages._experiment._chart_autogen import AUTO_OPEN_MAX_CHARTS
from dlboard.serve._pages._experiment._chart_editor import CHART_EDITOR_SAVE_ID
from dlboard.serve._pages._experiment._ids import METRIC_CONTENT_ID, PAGE_EXPERIMENT_ID
from dlboard.serve._pages._experiment._live_updates import (
    LIVE_PAUSED_BADGE_ID,
    LIVE_STATUS_ID,
    LIVE_UPDATES_ENABLED_ID,
)
from dlboard.serve._pages._experiment._notes import NOTES_COUNT_ID, NOTES_THREAD_ID
from dlboard.serve._pages._experiment._page_model import BasicExperimentPage
from dlboard.serve._pages._experiment._panel_view import (
    NEW_PANEL_ID,
    NEW_PANEL_NAME_ID,
    NEW_PANEL_OPEN_ID,
    NEW_TAB_BUTTON_ID,
    PANEL_AREA_ID,
    PANEL_FILTER_ID,
    PANEL_PAGE_SIZE_ID,
    RENAME_TAB_BUTTON_ID,
)
from dlboard.serve._pages._experiment._run_compare import (
    COMPARE_GRID_ID,
    COMPARE_MODE_ID,
    COMPARE_OPEN_ID,
    COMPARE_SEARCH_ID,
)
from dlboard.serve._pages._experiment._run_comparison_table import (
    NAVBAR_HPARAM_COL_SELECT_ID,
    NAVBAR_HPARAM_COLUMNS_TOGGLE_ID,
    NAVBAR_HPARAM_CONFIRM_COLS_ID,
    NAVBAR_HPARAM_DATATABLE_ID,
)
from dlboard.serve._pages._experiment._tab_controls import NEW_TAB_PANELS_SELECT_ID, RENAME_TAB_NAME_INPUT_ID
from dlboard.serve._pages._simple_homepage import NEW_PROJECT_BUTTON_ID, NEW_PROJECT_NAME_ID
from dlboard.serve._pages._simple_project_page import NEW_EXP_BUTTON_ID, NEW_EXP_NAME_ID
from dlboard.serve.app import PAGE_LOADING_CLASS

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from dash import Dash
    from playwright.sync_api import Locator, Page, Route

pytestmark = pytest.mark.browser


@pytest.fixture(autouse=True)
def _session_app_is_current(dlboard_app: Dash) -> Iterator[None]:
    """
    Read stores off `dlboard_app` in this module's helpers, not whichever app the process built last.

    Off the request thread there is no request to say which app is meant, and other test modules build
    apps of their own; with its Flask app context pushed, `get_system_data_store()` resolves this one.
    """
    with dlboard_app.server.app_context():
        yield


def _create_project_and_experiment(page: Page, live_server_url: str, name: str) -> None:
    """
    Drive the real homepage/project-page forms to create `name` for both project and experiment.

    Both tests in this module share one session-scoped app/db (see `dlboard_app`), so the homepage
    and project page each accumulate a card per test that's run before this one -- every lookup
    here is scoped to the card just created by `name`, not "the" project/experiment card.
    """
    page.goto(live_server_url)
    page.locator(f"#{NEW_PROJECT_NAME_ID}").fill(name)
    page.locator(f"#{NEW_PROJECT_BUTTON_ID}").click()
    page.get_by_role("link", name=name, exact=True).click()

    page.locator(f"#{NEW_EXP_NAME_ID}").fill(name)
    page.locator(f"#{NEW_EXP_BUTTON_ID}").click()


def _create_panel(page: Page, name: str) -> None:
    """Add an empty panel through the toolbar's "New panel" popover, and wait for it to show up."""
    page.locator(f"#{NEW_PANEL_OPEN_ID}").click()
    page.locator(f"#{NEW_PANEL_NAME_ID}").fill(name)
    page.locator(f"#{NEW_PANEL_ID}").click()
    expect(page.locator(".dl-panel-item-header", has_text=name)).to_be_visible()


def _wait_for_artifacts(experiment_id: int, count: int) -> None:
    """The server records an uploaded artifact asynchronously; a page loaded before it has would be live-patched."""
    store = get_system_data_store()
    deadline = time.monotonic() + 30
    while len(list(store.fetch_artifacts(experiment_id))) < count:
        assert time.monotonic() < deadline, "the artifacts were never recorded"
        time.sleep(0.1)


def _auto_generate_charts(page: Page) -> None:
    """
    Auto-generate an empty view's charts with the default grouping, confirming the preview.

    Returns once the preview modal has closed. The charts appear as the modal starts to fade out, and its
    backdrop stays over the page until the fade ends -- a pointer that moves onto the chart before then
    lands on the backdrop, never reaching the chart, so the tests that hover it need the modal gone first.
    """
    page.get_by_role("button", name="Auto-generate charts").click()
    page.get_by_role("button", name="Create charts").click()
    expect(page.get_by_role("dialog")).to_have_count(0)


def _open_panel_menu(page: Page, index: int = 0) -> None:
    """
    Open the actions menu on the `index`th panel header (its controls only mount once it opens).

    Waits for a menu that is still closing (picking an item closes it, with a fade) to be gone first: a click on its
    button while it fades would toggle it shut again, and the control the test reaches for would never appear.
    """
    menu = page.get_by_role("menu")
    expect(menu).to_have_count(0)
    page.locator(".dl-panel-item-header").nth(index).hover()
    page.get_by_role("button", name="Panel actions").nth(index).click()
    expect(menu).to_be_visible()


def test_logged_metrics_render_as_a_real_chart(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Create a project/experiment via the UI, log metrics through the real REST client, and confirm
    a chart actually renders -- not just that a `LineChart` component was returned with the right
    props (which `chart_render_isolation_test.py` already covers at the Python level), but that the
    JS chart library drew real SVG from it in a browser, after the "Auto-generate charts" action --
    none of which a Python-level test can see.
    """
    _create_project_and_experiment(page, live_server_url, "Browser Test Experiment")
    page.locator(".experiment-card").click()

    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=step,
                metrics={"loss": 1.0},
                timestamp_utc=pendulum.now("UTC"),
            )
            for step in range(3)
        ]
    )

    page.reload()
    _auto_generate_charts(page)
    # Nothing in the URL says which panels are open yet for a brand new experiment, so
    # `Paging.opened`'s own fallback opens the first (and here, only) panel by
    # default -- no click needed to see it render.

    # Scoped to the panel's own content area, not the whole page -- the panel header's own hover
    # controls (the tab-move `Select`'s dropdown chevron, the accordion chevron, ...) are also
    # `<svg>` elements, and would make this locator ambiguous if it searched the whole container.
    expect(page.locator(".dl-panel-body svg")).to_be_visible()
    assert console_errors == []


def test_slow_page_render_shows_a_loading_indicator(
    page: Page, live_server_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page whose `layout()` is slow shows a spinner until it renders, not a blank main area."""
    _create_project_and_experiment(page, live_server_url, "Slow Page Experiment")
    store = get_system_data_store()
    fetch_hyperparams = store.fetch_hyperparams

    def slow_fetch_hyperparams(experiment_id: int, **kwargs: Any) -> Iterator[models.HyperParams]:  # noqa: ANN401
        time.sleep(2)
        return fetch_hyperparams(experiment_id, **kwargs)

    monkeypatch.setattr(store, "fetch_hyperparams", slow_fetch_hyperparams)
    page.locator(".experiment-card").click()

    spinner = page.locator(f".{PAGE_LOADING_CLASS} .mantine-Loader-root")
    expect(spinner).to_be_visible()
    expect(page.locator(f"#{PAGE_EXPERIMENT_ID}")).to_be_visible(timeout=10_000)
    expect(spinner).to_be_hidden()


def test_panel_header_hover_controls_toggle_and_delete_without_disturbing_siblings(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Regression check for on-hover panel move/delete: `AccordionControl` now renders one level
    deeper, wrapped in a `Group` alongside sibling hover-revealed icons, instead of as a direct
    child of `AccordionItem` -- exactly the DOM/click-target interaction a Python-level
    component-tree test can't see. Proves (a) the control button still toggles open/closed despite
    the extra wrapping, (b) delete requires confirmation -- Cancel leaves the panel alone, Delete
    actually removes it -- and (c) doing so doesn't also toggle or otherwise disturb a different,
    currently-open panel.
    """
    _create_project_and_experiment(page, live_server_url, "Panel Hover Controls Experiment")
    page.locator(".experiment-card").click()

    for panel_name in ("keep", "delete-me"):
        _create_panel(page, panel_name)
        # Each click's callback bases its mutation on the client-side page-state snapshot, so the
        # next click must wait for this one's response to land first -- otherwise the second
        # request reads a stale (pre-mutation) snapshot and its response clobbers the first.
        expect(page.get_by_role("button", name=panel_name)).to_be_visible()

    # "keep" (created first) starts open -- nothing in the URL says which panels are open yet for a
    # brand new experiment, so `Paging.opened`'s own fallback opens the first panel
    # by default. "delete-me" (created second) isn't the fallback's pick, so it starts closed.
    # Round-trip "keep"'s control button itself: collapsing, re-expanding, then collapsing again
    # must all still work with the extra wrapping Group in place. Every panel header (open or
    # closed) carries its own hover-revealed "+", so that's no longer a usable open/closed signal
    # -- and `.dl-panel-body` itself collapses to zero height once "keep" (a brand new, chart-less
    # panel) has no content, so it's not reliable either. Mantine's own collapsible region
    # (`value="keep"` on the `AccordionItem`) is: its height/aria-hidden genuinely track
    # open/closed regardless of what's inside.
    keep_control = page.get_by_role("button", name="keep")
    # The accordion's own id is now a pattern-matching dict (one `Accordion` per tab group), so
    # Mantine's derived panel id is that dict's JSON string plus "-panel-<value>" -- not a clean
    # CSS identifier. Read the control's own `aria-controls` (which Mantine sets to that same
    # derived id) instead of hardcoding it, and match via an attribute selector (single-quoted,
    # since the id itself contains double quotes) rather than a `#id` selector.
    keep_panel_id = keep_control.get_attribute("aria-controls")
    keep_region = page.locator(f"[id='{keep_panel_id}']")
    expect(keep_region).to_be_visible()
    keep_control.click()
    expect(keep_region).not_to_be_visible()
    keep_control.click()
    expect(keep_region).to_be_visible()
    keep_control.click()
    expect(keep_region).not_to_be_visible()

    # "delete-me" is second in document order and was never opened.
    _open_panel_menu(page, 1)
    page.get_by_role("menuitem", name="Delete panel…").click()

    confirm_text = page.get_by_text("Delete this panel?")
    expect(confirm_text).to_be_visible()
    page.get_by_role("button", name="Cancel").click()
    expect(confirm_text).not_to_be_visible()
    expect(page.get_by_text("delete-me")).to_be_visible()  # Cancel left it alone

    _open_panel_menu(page, 1)
    page.get_by_role("menuitem", name="Delete panel…").click()
    expect(confirm_text).to_be_visible()
    page.get_by_role("button", name="Delete", exact=True).click()
    expect(page.get_by_text("delete-me")).to_have_count(0)
    expect(
        keep_region
    ).not_to_be_visible()  # "keep" (left closed above) is untouched by its sibling's deletion
    assert console_errors == []


def test_delete_experiment_and_restore_from_admin_trash(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Delete an experiment through its confirmation modal, confirm it's gone from the project page,
    then restore it from the admin Trash tab and confirm it's back -- the cross-page wiring between
    the delete flow and the admin page is only proven end to end by actually navigating both.
    """
    _create_project_and_experiment(page, live_server_url, "Delete Me Experiment")

    project_url = page.url
    page.locator(".experiment-card").click()
    page.get_by_role("button", name="Delete experiment").click()
    page.get_by_role("button", name="Delete", exact=True).click()

    expect(page).to_have_url(project_url)
    expect(page.locator(".experiment-card")).to_have_count(0)

    page.goto(f"{live_server_url}/admin")
    page.get_by_role("button", name="Restore").click()
    expect(page.get_by_role("button", name="Restore")).to_have_count(0)

    page.goto(project_url)
    expect(page.locator(".experiment-card")).to_have_count(1)
    assert console_errors == []


def _panel_order(page: Page) -> list[str]:
    return page.locator(".dl-panel-item-header").evaluate_all(
        "els => els.map(el => el.querySelector('.dl-panel-drag-handle').getAttribute('data-panel-name'))"
    )


def _wait_until[T](get_actual: Callable[[], list[T]], expected: list[T]) -> None:
    """
    Poll `get_actual()` until it equals `expected` or ~5s pass.

    A completed drop only fires `set_props` on the client -- the actual reorder is a server round
    trip after that, so a bare one-shot `assert` right after `drag_to()` returns is a real race
    (usually wins, occasionally doesn't, especially under load). `expect(locator)...` has this same
    built-in retry for DOM-based assertions; this is the equivalent for a value read off the
    server's own store instead of the page.
    """
    deadline = time.monotonic() + 5
    actual = get_actual()
    while actual != expected and time.monotonic() < deadline:
        time.sleep(0.1)
        actual = get_actual()
    assert actual == expected


def test_drag_and_drop_reorders_panels(page: Page, live_server_url: str, console_errors: list[str]) -> None:
    """
    Drag the third panel's handle onto the first panel and drop it -- the only way to prove the
    native HTML5 drag/drop wiring (`_experiment_page_dragdrop.js`, `PANEL_REORDER_STORE_ID`) works
    end to end: a Python-level test can render the resulting markup but can't simulate an actual
    browser drag gesture or its `dragover`/`drop` event flow.
    """
    _create_project_and_experiment(page, live_server_url, "Drag Reorder Experiment")
    page.locator(".experiment-card").click()

    for panel_name in ("alpha", "beta", "gamma"):
        _create_panel(page, panel_name)
        expect(page.get_by_role("button", name=panel_name)).to_be_visible()

    assert _panel_order(page) == ["alpha", "beta", "gamma"]

    source = page.locator('.dl-panel-drag-handle[data-panel-name="gamma"]')
    target = page.locator('.dl-panel-item-header:has-text("alpha")')
    source.hover()
    # `.dl-panel-controls`'s hover reveal is CSS-transitioned (120ms) -- give it time to actually
    # reach opacity:1 before dragging, or the drag intermittently fails to register (flaky without
    # this, especially under load: the handle is dragged mid-fade).
    page.wait_for_timeout(200)
    # Dropping in the target's top half requests "before" (see `_experiment_page_dragdrop.js`).
    target_box = target.bounding_box()
    assert target_box is not None
    source.drag_to(target, target_position={"x": target_box["width"] / 2, "y": 2})

    expect(page.locator(".dl-panel-item-header")).to_have_count(3)
    _wait_until(lambda: _panel_order(page), ["gamma", "alpha", "beta"])
    assert console_errors == []


def _current_page_model(page: Page, experiment_id: int) -> BasicExperimentPage:
    """
    The page (shared, or whichever view the browser is currently on) the UI is actually showing.

    A structural edit branches into a view of the acting user's own the moment it happens (see
    `_page_repository.save_page`), so a helper that always reads the shared page stops
    matching what's on screen the instant that first happens -- read `page`'s own `?view=` instead
    of assuming which one applies.
    """
    query = page.url.split("?", 1)[1] if "?" in page.url else ""
    view_id = next(
        (int(part.removeprefix("view=")) for part in query.split("&") if part.startswith("view=")), None
    )
    store = get_system_data_store()
    if view_id is not None:
        view = store.get_view(BasicExperimentPage, view_id)
        if view is not None:
            return cast("BasicExperimentPage", view)
    return cast(
        "BasicExperimentPage", store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    )


def _ungrouped_chart_columns(page: Page, experiment_id: int) -> list[str]:
    """
    The "Ungrouped" panel's charts' `column` params, in order -- reads the persisted page directly,
    since a chart's rendered `data-chart-index` alone can't distinguish *which* chart (they're
    always contiguous 0..N-1 post-render) moved where. Safe to call off the request thread:
    `_session_app_is_current` makes `get_system_data_store()` resolve `dlboard_app` here.
    """
    panel = next(p for p in _current_page_model(page, experiment_id).panels if p.name == "Ungrouped")
    return [str(c.parameters["column"]) for c in panel.charts]


def test_drag_and_drop_reorders_charts_within_a_panel(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Same proof as `test_drag_and_drop_reorders_panels`, one level down: dragging a chart's handle
    onto a sibling chart in the same panel reorders just those two, via `CHART_REORDER_STORE_ID`.
    """
    _create_project_and_experiment(page, live_server_url, "Drag Reorder Charts Experiment")
    page.locator(".experiment-card").click()

    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    # No delimiter in any of these names, so auto-generate collapses them into one "Ungrouped"
    # panel with three charts, alphabetically: accuracy, loss, lr.
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=0,
                metrics={"loss": 1.0, "accuracy": 0.5, "lr": 0.01},
                timestamp_utc=pendulum.now("UTC"),
            )
        ]
    )

    page.reload()
    _auto_generate_charts(page)
    # Nothing in the URL says which panels are open yet for a brand new experiment, so
    # `Paging.opened`'s own fallback opens the first (and here, only) panel by
    # default -- no click needed for its charts (and their drag handles) to render.
    expect(page.locator(".dl-chart-drag-handle")).to_have_count(3)
    # Auto-generating on the shared page branches into a view of your own (see `save_page`) --
    # wait for that branch's `?view=` to land before reading `_ungrouped_chart_columns` off it,
    # or it's still reading the (now-untouched) shared page the branch just moved away from.
    expect(page).to_have_url(re.compile(r"\?view=\d+$"))
    assert _ungrouped_chart_columns(page, experiment_id) == ["accuracy", "loss", "lr"]

    # Drag the last chart (index 2, "lr") to just before the first (index 0, "accuracy") -- drop
    # near its drag handle (top-left, in the controls row) rather than over its rendered chart SVG,
    # which captures its own pointer events and would swallow the drop before it ever bubbles up
    # to the delegated `document` listener in `_experiment_page_dragdrop.js`.
    # The 3 stacked charts don't all fit in the default viewport, and a drag gesture needs both
    # endpoints on-screen simultaneously (no mid-drag auto-scroll) -- widen it so source and target
    # are both visible without scrolling.
    page.set_viewport_size({"width": 1280, "height": 1600})
    source = page.locator('.dl-chart-drag-handle[data-panel-name="Ungrouped"][data-chart-index="2"]')
    target = page.locator('.dl-chart-item:has(.dl-chart-drag-handle[data-chart-index="0"])')
    source.hover()
    # See the matching wait in `test_drag_and_drop_reorders_panels` -- same hover-reveal transition.
    page.wait_for_timeout(200)
    source.drag_to(target, target_position={"x": 2, "y": 5})

    expect(page.locator(".dl-chart-drag-handle")).to_have_count(3)
    _wait_until(lambda: _ungrouped_chart_columns(page, experiment_id), ["lr", "accuracy", "loss"])
    assert console_errors == []


def test_drag_and_drop_moves_a_chart_into_a_different_panel(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    A chart's drag handle used to only ever reorder it within its own panel (or move it to a tab) --
    there was no way to move a chart into a *different* panel without deleting and recreating it.
    Covers both new drop targets `_experiment_page_dragdrop.js` adds for that: dropping onto one of
    the target panel's existing charts (inserts next to it) and, separately, onto its otherwise-empty
    body (appends).
    """
    _create_project_and_experiment(page, live_server_url, "Move Chart Between Panels Experiment")
    page.locator(".experiment-card").click()

    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    # A "/" prefix in each name splits these into two panels, "train" and "val", one chart each --
    # auto-generate would otherwise collapse same-named metrics into one shared panel.
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=0,
                metrics={"train/loss": 1.0, "val/loss": 0.5},
                timestamp_utc=pendulum.now("UTC"),
            )
        ]
    )
    page.reload()
    _auto_generate_charts(page)
    # Auto-generating on the shared page branches into a view of your own (see `save_page`) --
    # wait for that branch's `?view=` to land before anything below reads the page off it.
    expect(page).to_have_url(re.compile(r"\?view=\d+$"))
    # Nothing in the URL says which panels are open yet for a brand new experiment, so
    # `Paging.opened`'s own fallback already opens the first panel ("train" --
    # metrics dict insertion order is preserved through to panel-creation order) -- only "val"
    # needs a click.
    page.get_by_role("button", name="val", exact=True).click()
    expect(page.locator(".dl-chart-drag-handle")).to_have_count(2)
    assert _panel_chart_columns(page, experiment_id, "train") == ["train/loss"]
    assert _panel_chart_columns(page, experiment_id, "val") == ["val/loss"]

    # Create the empty "extra" panel *before* any drag -- a completed drop triggers its own
    # full-page re-render (a separate response from the `drag_to()` call that requested it), and
    # doing this fill()/click() while that's still in flight can land on a stale, about-to-be-
    # replaced input node. Doing it up front instead sidesteps that race entirely.
    _create_panel(page, "extra")
    expect(page.get_by_role("button", name="extra")).to_be_visible()
    page.get_by_role("button", name="extra").click()

    # A drag gesture needs both endpoints on-screen simultaneously -- auto-scroll (see
    # `test_dragging_near_the_top_of_the_viewport_auto_scrolls_the_page`) isn't fast enough to keep
    # up with a single synthetic `drag_to` -- so widen the viewport up front rather than relying on
    # it here, same as the same-panel reorder tests above.
    page.set_viewport_size({"width": 1280, "height": 2000})

    # Drop onto "val"'s existing chart -- inserts there rather than reordering (source and target
    # are different panels), landing before it since the drop is in its left half.
    source = page.locator('.dl-chart-drag-handle[data-panel-name="train"][data-chart-index="0"]')
    target = page.locator('.dl-chart-item:has(.dl-chart-drag-handle[data-panel-name="val"])')
    source.hover()
    page.wait_for_timeout(200)  # matches the other drag tests' wait for the hover-reveal transition
    source.drag_to(target, target_position={"x": 2, "y": 5})

    _wait_until(lambda: _panel_chart_columns(page, experiment_id, "val"), ["train/loss", "val/loss"])
    assert _panel_chart_columns(page, experiment_id, "train") == []
    # `_wait_until` only confirms the backend has the move -- the drop's own re-render can still be
    # in flight. Wait for the DOM to catch up before starting the next drag on top of it.
    expect(page.locator(".dl-chart-drag-handle")).to_have_count(2)

    # Drop onto "extra"'s (empty) body -- appends, since there's no chart there to land next to.
    source = page.locator('.dl-chart-drag-handle[data-panel-name="val"][data-chart-index="0"]')
    target = page.locator('.dl-panel-body[data-panel-name="extra"]')
    source.hover()
    page.wait_for_timeout(200)
    source.drag_to(target)

    _wait_until(lambda: _panel_chart_columns(page, experiment_id, "extra"), ["train/loss"])
    assert _panel_chart_columns(page, experiment_id, "val") == ["val/loss"]
    assert console_errors == []


def test_navbar_columns_picker_applies_a_selected_column(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Regression check for the navbar run-comparison grid (ag-grid): the Columns picker is a
    `dmc.Popover` wrapping a `MultiSelect`, and its own dropdown used to render in a *separate*
    portal from the outer Popover's -- so clicking an option read as a click *outside* the Popover
    and closed the whole thing before "Apply" was ever reachable, discarding the pick. Also proves
    ag-grid doesn't warn about a duplicate/undefined row id along the way (a real row-identity bug:
    `assert console_errors == []` alone can't catch it, since ag-grid logs it via `console.warn`,
    not `console.error`) and that rebuilding the grid doesn't leave a phantom empty row.
    """
    _create_project_and_experiment(page, live_server_url, "Columns Picker Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])

    api = BasicDlboardAPI(live_server_url)
    for _ in range(2):
        run = api.create_run(models.NewRun(experiment_id=experiment_id))
        api.log_metric_batch(
            [
                models.LoggedMetrics(
                    experiment_id=experiment_id,
                    run_id=run.id,
                    step=0,
                    metrics={"accuracy": 0.5},
                    timestamp_utc=pendulum.now("UTC"),
                )
            ]
        )
    page.reload()

    ag_grid_warnings: list[str] = []
    page.on(
        "console",
        lambda msg: (
            # pyrefly: ignore [bad-argument-type]
            ag_grid_warnings.append(msg.text) if "AG Grid" in msg.text else None
        ),
    )

    grid = page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID}")
    expect(grid.locator(".ag-center-cols-container .ag-row")).to_have_count(2)

    page.locator(f"#{NAVBAR_HPARAM_COLUMNS_TOGGLE_ID}").click()
    page.locator(f"#{NAVBAR_HPARAM_COL_SELECT_ID}").click()
    page.get_by_role("option", name="accuracy").click()

    apply_button = page.locator(f"#{NAVBAR_HPARAM_CONFIRM_COLS_ID}")
    expect(apply_button).to_be_visible()
    apply_button.click()

    expect(grid.get_by_role("columnheader", name="accuracy")).to_be_visible()
    # Rebuilding the grid for the new column mustn't leave a stray extra row -- still exactly 2.
    expect(grid.locator(".ag-center-cols-container .ag-row")).to_have_count(2)

    assert console_errors == []
    assert ag_grid_warnings == []


def test_assign_panel_to_a_new_tab_and_switch_back(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    End-to-end regression check for grouping panels into tabs, driving all three entry points:
    the "+" button (the *only* place a brand-new tab gets created), dragging a panel onto an
    already-existing tab to move it there, and renaming the active tab. Mantine's `Tabs` previously
    refused to render the default/ungrouped tab at all (it silently rejects an empty-string
    `value`), so the moment a second tab existed there was no way to click back to the original
    panels -- proves both tabs stay reachable, each shows only its own panels, drag-to-move and
    rename both work, and it all survives a reload.
    """
    _create_project_and_experiment(page, live_server_url, "Panel Tabs Experiment")
    page.locator(".experiment-card").click()

    for panel_name in ("keep", "tabbed"):
        _create_panel(page, panel_name)
        expect(page.get_by_role("button", name=panel_name)).to_be_visible()

    # No tabs assigned yet -- every panel shares the one default tab, so there's no tab bar at all,
    # just the lone "+" button that's always available to start one.
    expect(page.get_by_role("tab")).to_have_count(0)
    new_tab_button = page.locator(f"#{NEW_TAB_BUTTON_ID}")
    expect(new_tab_button).to_be_visible()

    # Create a new "Images" tab with "tabbed" pre-selected into it -- the only way to create a tab.
    new_tab_button.click()
    page.get_by_role("dialog").get_by_role("textbox").first.fill("Images")
    page.locator(f"#{NEW_TAB_PANELS_SELECT_ID}").click()
    page.get_by_role("option", name="tabbed").click()
    page.keyboard.press("Escape")  # close the MultiSelect dropdown -- it's covering the Create button
    page.get_by_role("button", name="Create", exact=True).click()

    images_tab = page.get_by_role("tab", name="Images")
    general_tab = page.get_by_role("tab", name="General")
    expect(images_tab).to_be_visible()
    expect(general_tab).to_be_visible()

    # Creating a tab jumps straight to it: "tabbed" is visible, "keep" (still on "General") isn't.
    expect(page.get_by_role("button", name="tabbed")).to_be_visible()
    expect(page.get_by_role("button", name="keep")).not_to_be_visible()

    # The rename-tab icon is disabled on "General" -- nothing there to rename.
    rename_tab_button = page.locator(f"#{RENAME_TAB_BUTTON_ID}")
    expect(rename_tab_button).to_be_enabled()

    # Switching back to "General" is exactly the flow that used to be impossible.
    general_tab.click()
    expect(page.get_by_role("button", name="keep")).to_be_visible()
    expect(page.get_by_role("button", name="tabbed")).not_to_be_visible()
    expect(rename_tab_button).to_be_disabled()

    # Move "keep" into the *existing* "Images" tab by dragging it there -- no typing, no button.
    # The drop also switches straight to "Images": both its panels are visible with no extra click.
    keep_header = page.locator(".dl-panel-item-header", has_text="keep")
    keep_handle = keep_header.locator(".dl-panel-drag-handle")
    keep_header.hover()
    page.wait_for_timeout(200)  # matches the drag tests' own wait for the hover-reveal transition
    keep_handle.drag_to(images_tab)
    expect(page.get_by_role("button", name="tabbed")).to_be_visible()
    expect(page.get_by_role("button", name="keep")).to_be_visible()

    # Rename the active tab ("Images") -- both panels that were on it follow the rename.
    rename_tab_button.click()
    page.locator(f"#{RENAME_TAB_NAME_INPUT_ID}").fill("Screenshots")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_role("tab", name="Screenshots")).to_be_visible()
    expect(page.get_by_role("tab", name="Images")).to_have_count(0)
    expect(page.get_by_role("button", name="tabbed")).to_be_visible()
    expect(page.get_by_role("button", name="keep")).to_be_visible()

    # The grouping, the rename, and which tab was active all survive a reload.
    page.reload()
    expect(page.get_by_role("tab", name="Screenshots")).to_be_visible()
    expect(page.get_by_role("button", name="tabbed")).to_be_visible()
    expect(page.get_by_role("button", name="keep")).to_be_visible()

    assert console_errors == []


def _panel_chart_columns(page: Page, experiment_id: int, panel_name: str) -> list[str]:
    """Like `_ungrouped_chart_columns`, but for any named panel."""
    panel = next(p for p in _current_page_model(page, experiment_id).panels if p.name == panel_name)
    return [str(c.parameters["column"]) for c in panel.charts]


def test_new_tab_without_panels_gets_an_empty_panel_that_accepts_a_dragged_chart(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Two related gaps in one flow: creating a tab used to require moving an existing whole panel
    into it (there was no way to end up with an empty tab to drop things into later), and a chart
    could only ever be reordered within its own panel -- moving just one chart elsewhere meant
    deleting and recreating it. Proves both: leaving the new-tab panel picker empty still produces a
    real (empty) panel in that tab, and dragging a chart's handle onto a tab (not just a panel's)
    moves that one chart into it -- and switches straight to that tab, rather than leaving the
    chart you just dragged out of view on the tab you dragged it from.
    """
    _create_project_and_experiment(page, live_server_url, "Chart To Tab Experiment")
    page.locator(".experiment-card").click()

    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=0,
                metrics={"loss": 1.0},
                timestamp_utc=pendulum.now("UTC"),
            )
        ]
    )
    page.reload()
    _auto_generate_charts(page)
    # The rebuild that lands the generated panels also re-renders the new-tab button; wait for it
    # (the auto-generate button only exists on a view with no panels) so the click below can't hit
    # the outgoing copy.
    expect(page.get_by_role("button", name="Auto-generate charts")).to_have_count(0)
    # Auto-generating on the shared page branches into a view of your own (see `save_page`) --
    # wait for that branch's `?view=` to land before anything below reads the page off it.
    expect(page).to_have_url(re.compile(r"\?view=\d+$"))

    # Create "Images" without picking any panel -- the picker stays empty.
    page.locator(f"#{NEW_TAB_BUTTON_ID}").click()
    page.get_by_role("dialog").get_by_role("textbox").first.fill("Images")
    page.get_by_role("button", name="Create", exact=True).click()

    images_tab = page.get_by_role("tab", name="Images")
    general_tab = page.get_by_role("tab", name="General")
    expect(images_tab).to_be_visible()
    # A real, empty panel exists in the new tab -- not just an empty tab with nothing to drop into.
    expect(page.get_by_role("button", name="Images", exact=True)).to_be_visible()
    assert _panel_chart_columns(page, experiment_id, "Images") == []

    # Drag the "loss" chart (in "Ungrouped", on "General") onto the "Images" tab.
    general_tab.click()
    # "Ungrouped" is already open -- it was the first (and, at auto-generate time, only) panel, so
    # `Paging.opened`'s fallback opened it by default; no click needed.
    handle = page.locator('.dl-chart-drag-handle[data-panel-name="Ungrouped"][data-chart-index="0"]')
    handle.hover()
    page.wait_for_timeout(200)  # matches the other drag tests' wait for the hover-reveal transition
    handle.drag_to(images_tab)

    _wait_until(lambda: _panel_chart_columns(page, experiment_id, "Images"), ["loss"])
    assert _panel_chart_columns(page, experiment_id, "Ungrouped") == []
    # The drag switched the active tab to "Images" -- its (now non-empty) panel is visible without
    # having to click the tab, and "Ungrouped" (still on "General") isn't.
    expect(page.get_by_role("button", name="Images", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Ungrouped")).not_to_be_visible()
    assert console_errors == []


def test_dragging_near_the_top_of_the_viewport_auto_scrolls_the_page(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    A panel/chart dragged from below the fold couldn't reach the tab bar at the top of the page at
    all -- native HTML5 drag/drop doesn't auto-scroll on its own, and there was no code filling that
    gap. `_experiment_page_dragdrop.js` now scrolls the page itself while the cursor sits near a
    viewport edge during a drag.

    Driving this through `Locator.drag_to` can't prove it: Playwright scrolls the drop target into
    view *before* starting the drag, which would silently mask a broken auto-scroll. Dispatching the
    real `dragstart`/`dragover` events by hand instead -- Playwright's own documented recipe for
    drag-and-drop it can't drive natively -- keeps this a genuine test of the page's own scroll
    response, not of Playwright doing the scrolling for it.
    """
    _create_project_and_experiment(page, live_server_url, "Auto Scroll Experiment")
    page.locator(".experiment-card").click()

    _create_panel(page, "only-panel")
    handle = page.locator(".dl-panel-drag-handle").first
    expect(handle).to_be_visible()

    page.set_viewport_size({"width": 1280, "height": 500})
    # Real content isn't reliably tall enough on its own to need scrolling -- pad it so there's
    # somewhere for the auto-scroll to actually move the page to.
    page.evaluate("document.body.style.paddingBottom = '2000px'")
    page.evaluate("window.scrollTo(0, 800)")
    start_scroll_y = page.evaluate("window.scrollY")
    assert start_scroll_y > 0

    page.evaluate("""() => {
        const dt = new DataTransfer();
        document.querySelector(".dl-panel-drag-handle").dispatchEvent(
            new DragEvent("dragstart", { bubbles: true, cancelable: true, dataTransfer: dt })
        );
    }""")
    # clientY near the top edge -- inside `EDGE_SCROLL_ZONE_PX`, so the auto-scroll loop scrolls up.
    page.evaluate("""() => {
        document.dispatchEvent(
            new DragEvent("dragover", { bubbles: true, cancelable: true, clientY: 20 })
        );
    }""")
    # The scroll itself happens over several `requestAnimationFrame` ticks, not instantly on the
    # `dragover` above -- give it real wall-clock time to actually run a handful of them.
    page.wait_for_timeout(300)
    page.evaluate('document.dispatchEvent(new DragEvent("dragend", { bubbles: true }))')

    assert page.evaluate("window.scrollY") < start_scroll_y
    assert console_errors == []


def _toolbar_clearance(page: Page) -> float:
    """The gap between the bottom of the panel toolbar and the top of the first visible panel."""
    button_box = page.locator(f"#{NEW_PANEL_OPEN_ID}").bounding_box()
    panel_box = page.locator(".mantine-Accordion-item:visible").first.bounding_box()
    assert button_box is not None
    assert panel_box is not None
    return panel_box["y"] - (button_box["y"] + button_box["height"])


def test_panel_area_layout_is_not_squeezed_by_the_new_panel_controls(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Geometry-based regression check for real layout bugs. A role/text-based test can't catch these
    (every control is still present and labelled correctly; only positions and widths are wrong),
    so this measures bounding boxes instead:

    - The panel/tab controls once lived in a container *beside* the accordion/tabs tree, squeezing
      it into sharing a row -- it must span (nearly) the page's full content width.
    - The panel actions sit on the very same row as the tab bar, not a separate row below it, and
      "New tab" sits right next to the tabs themselves, not clear across the row.
    - The toolbar once butted straight up against the first panel's border -- with or without tabs,
      it must leave a visible gap above the panels.
    """
    _create_project_and_experiment(page, live_server_url, "Layout Regression Experiment")
    page.locator(".experiment-card").click()

    for panel_name in ("keep", "tabbed"):
        _create_panel(page, panel_name)
    assert _toolbar_clearance(page) >= 8

    # Tab the second panel so a real tab bar is on screen too.
    page.locator(f"#{NEW_TAB_BUTTON_ID}").click()
    page.get_by_role("dialog").get_by_role("textbox").first.fill("Images")
    page.locator(f"#{NEW_TAB_PANELS_SELECT_ID}").click()
    page.get_by_role("option", name="tabbed").click()
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Create", exact=True).click()
    images_tab = page.get_by_role("tab", name="Images")
    expect(images_tab).to_be_visible()
    assert _toolbar_clearance(page) >= 8

    page_box = page.locator(f"#{PAGE_EXPERIMENT_ID}").bounding_box()
    metric_content_box = page.locator(f"#{METRIC_CONTENT_ID}").bounding_box()
    tab_box = images_tab.bounding_box()
    new_tab_button_box = page.locator(f"#{NEW_TAB_BUTTON_ID}").bounding_box()
    new_panel_button_box = page.locator(f"#{NEW_PANEL_OPEN_ID}").bounding_box()
    assert page_box is not None
    assert metric_content_box is not None
    assert tab_box is not None
    assert new_tab_button_box is not None
    assert new_panel_button_box is not None

    assert metric_content_box["width"] >= page_box["width"] * 0.9
    assert new_tab_button_box["x"] - (tab_box["x"] + tab_box["width"]) < 150
    tab_center_y = tab_box["y"] + tab_box["height"] / 2
    button_center_y = new_panel_button_box["y"] + new_panel_button_box["height"] / 2
    assert abs(tab_center_y - button_center_y) < 10

    assert console_errors == []


def test_switching_tabs_does_not_remount_the_navbar_run_table(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Regression check: switching tabs persists the active tab by writing to the same shared
    page-state store the navbar's run-comparison grid (`_run_comparison_table.py`) used to
    unconditionally rebuild itself from on *any* change to that store -- so every tab click tore
    down and remounted the whole ag-grid, a visible flicker unrelated to anything the grid actually
    displays (it doesn't even show tab data). A role/text-based assertion can't catch this -- the
    grid shows the same rows/columns either way -- so this tags the grid's own DOM node before
    switching tabs and proves that exact node (not a freshly rendered lookalike) is still there
    after, rather than counting requests or checking visible content.
    """
    _create_project_and_experiment(page, live_server_url, "Navbar Flicker Experiment")
    page.locator(".experiment-card").click()

    for panel_name in ("keep", "tabbed"):
        _create_panel(page, panel_name)
        expect(page.get_by_role("button", name=panel_name)).to_be_visible()

    page.locator(f"#{NEW_TAB_BUTTON_ID}").click()
    page.get_by_role("dialog").get_by_role("textbox").first.fill("Images")
    page.locator(f"#{NEW_TAB_PANELS_SELECT_ID}").click()
    page.get_by_role("option", name="tabbed").click()
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Create", exact=True).click()

    general_tab = page.get_by_role("tab", name="General")
    images_tab = page.get_by_role("tab", name="Images")
    expect(images_tab).to_be_visible()

    grid_root = page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID} .ag-root-wrapper")
    expect(grid_root).to_be_visible()
    grid_root.evaluate("el => el.setAttribute('data-marker', 'untouched')")

    general_tab.click()
    images_tab.click()

    assert grid_root.get_attribute("data-marker") == "untouched"
    assert console_errors == []


def test_each_run_is_drawn_in_its_own_palette_color_matching_its_run_table_swatch(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Runs are colored by `series_color` -- CSS variables, not literal colors -- so this checks the
    part a Python test can't: that the variables actually resolve through Recharts' SVG to real,
    distinct stroke colors, one per run -- and that the navbar run table labels each run with the
    very same color, so it reads as the charts' legend.
    """
    _create_project_and_experiment(page, live_server_url, "Palette Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    for _ in range(3):
        run = api.create_run(models.NewRun(experiment_id=experiment_id))
        api.log_metric_batch(
            [
                models.LoggedMetrics(
                    experiment_id=experiment_id,
                    run_id=run.id,
                    step=step,
                    metrics={"loss": 1.0 / (step + 1)},
                    timestamp_utc=pendulum.now("UTC"),
                )
                for step in range(3)
            ]
        )
    page.reload()
    _auto_generate_charts(page)

    curves = page.locator(".dl-panel-body .recharts-line-curve")
    expect(curves).to_have_count(3)
    strokes = curves.evaluate_all("paths => paths.map(p => getComputedStyle(p).stroke)")
    assert len(set(strokes)) == 3
    assert all(stroke.startswith("rgb(") for stroke in strokes), strokes
    swatch_cells = page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID} .dl-swatch")
    expect(swatch_cells).to_have_count(3)
    swatches = swatch_cells.evaluate_all(
        "cells => cells.map(c => getComputedStyle(c, '::before').backgroundColor)"
    )
    assert sorted(swatches) == sorted(strokes)
    assert console_errors == []


def test_chart_legend_labels_each_run_by_name_matching_its_run_table_row(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """The line chart's legend reads a run's auto-generated name, not its bare id -- same name the
    navbar run table already shows for that run."""
    _create_project_and_experiment(page, live_server_url, "Chart Legend Names Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    for _ in range(3):
        run = api.create_run(models.NewRun(experiment_id=experiment_id))
        api.log_metric_batch(
            [
                models.LoggedMetrics(
                    experiment_id=experiment_id,
                    run_id=run.id,
                    step=step,
                    metrics={"loss": 1.0 / (step + 1)},
                    timestamp_utc=pendulum.now("UTC"),
                )
                for step in range(3)
            ]
        )
    page.reload()
    _auto_generate_charts(page)

    legend_items = page.locator(".mantine-ChartLegend-legendItemName")
    expect(legend_items).to_have_count(3)
    legend_names = set(legend_items.all_text_contents())
    table_names = set(
        page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID} .ag-cell[col-id='run_name']").all_text_contents()
    )
    assert legend_names == table_names
    assert not any(name.startswith("Run ") for name in legend_names), legend_names
    assert console_errors == []


def test_chart_tooltip_shows_every_series_at_every_hovered_x_position(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Regression test for inconsistent hover tooltips.

    `LineChart.render()` pivots each run's logged points into its own column, indexed by x-value
    (`line_chart.py`'s `df.pivot(index=x_axis, columns="run_id", ...)`); any (x, run) combination a
    run never logged at -- routine the moment two runs have different step counts or get sampled
    differently, not a contrived edge case -- becomes `NaN` in that pivoted table. Recharts' default
    tooltip only lists a series whose value at the *exact* hovered row is non-`NaN`, so which runs
    show up depends entirely on which exact x happens to get hovered -- series visibly pop in and
    out as the cursor moves, even though every run has real data spanning the whole visible range.

    Expected behavior: hovering any x position within a series' own logged range shows that
    series' *closest* value, consistently, not "whichever run happened to log exactly this x" --
    and since a closest value isn't necessarily *from* the hovered x, that run's tooltip row says
    where it came from (e.g. "@ 3") whenever it was filled in from elsewhere.
    """
    _create_project_and_experiment(page, live_server_url, "Tooltip Consistency Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])

    api = BasicDlboardAPI(live_server_url)
    run_a = api.create_run(models.NewRun(experiment_id=experiment_id))
    run_b = api.create_run(models.NewRun(experiment_id=experiment_id))
    # Run A logs every step 0-9; Run B only every third step -- a routine mismatch (different
    # epoch counts, different sampling rates), not an artificial edge case.
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run_a.id,
                step=step,
                metrics={"loss": 1.0 - step * 0.05},
                timestamp_utc=pendulum.now("UTC"),
            )
            for step in range(10)
        ]
    )
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run_b.id,
                step=step,
                metrics={"loss": 1.0 - step * 0.03},
                timestamp_utc=pendulum.now("UTC"),
            )
            for step in (0, 3, 6, 9)
        ]
    )
    page.reload()
    _auto_generate_charts(page)
    # Nothing in the URL says which panels are open yet for a brand new experiment, so
    # `Paging.opened`'s own fallback opens the first (and here, only) panel by
    # default -- no click needed to see it render.

    svg = page.locator(".dl-panel-body svg").first
    expect(svg).to_be_visible()

    # Auto-generated -- never actually None here, just typed that way.
    assert run_a.name is not None
    assert run_b.name is not None
    expected_series = sorted([run_a.name, run_b.name])

    # Both runs' data spans this entire x range (steps 0-9) -- every position across it must show
    # both series, not just whichever run happened to log that exact step.
    saw_a_source_annotation = False
    for frac in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
        # Measured at each hover, not once up front: the chart may still be settling its size when it first
        # becomes visible, which would put the cursor off the position this iteration means to test.
        box = svg.bounding_box()
        assert box is not None
        x = box["x"] + box["width"] * frac
        y = box["y"] + box["height"] * 0.5
        # Two moves, not one -- Recharts only recomputes the hovered index on a real position
        # change, and the cursor may already be sitting at this exact pixel from a previous frac.
        page.mouse.move(x - 1, y)
        page.mouse.move(x, y)
        # Polled, not read after a fixed sleep: the tooltip is drawn a render after the pointer moves, which a
        # slow runner can take longer than any sleep we would pick.
        _wait_until(
            lambda: sorted(page.locator(".dl-chart-tooltip-name").all_text_contents()), expected_series
        )

        # Run B only logged every third step, so most hovered positions show *its* value filled in
        # from a nearby step, not the exact hovered one -- its row must say so ("@ 3"), never
        # implying it was logged exactly where the cursor is.
        if page.locator(".dl-chart-tooltip-source").count():
            saw_a_source_annotation = True

    assert saw_a_source_annotation, (
        "expected at least one hover position to annotate a filled-in value's source x"
    )
    assert console_errors == []


def test_live_update_poll_shows_a_new_run_without_a_reload(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    End-to-end proof of the live-update poll (`poll_for_updates`, driven by `LIVE_POLL_INTERVAL_ID`):
    a run started purely through the backend -- exactly how a real training process talks to
    dlboard, with the browser tab never touched -- must show up in the navbar run-comparison table
    on its own, with no `page.reload()`. It must also do so *without* remounting the panel: this
    tags the "Ungrouped" panel's own content node (`.dl-panel-body`, the same DOM node
    `poll_for_updates` patches via individual `CHART_CONTENT.of(...)` nodes inside it) before the
    second run exists, then proves that exact node -- not a freshly rendered lookalike -- is still
    there afterward, the same "still the same node" proof
    `test_switching_tabs_does_not_remount_the_navbar_run_table` uses for the sibling flicker
    regression this feature was built to avoid.
    """
    _create_project_and_experiment(page, live_server_url, "Live Update Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])

    api = BasicDlboardAPI(live_server_url)
    first_run = api.create_run(models.NewRun(experiment_id=experiment_id))
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=first_run.id,
                step=0,
                metrics={"loss": 1.0},
                timestamp_utc=pendulum.now("UTC"),
            )
        ]
    )
    page.reload()
    _auto_generate_charts(page)
    # Nothing in the URL says which panels are open yet for a brand new experiment, so
    # `Paging.opened`'s own fallback already opened "Ungrouped" by default -- the
    # poll only patches currently-open panels' charts, so it has one to patch without a click.

    grid = page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID}")
    expect(grid.locator(".ag-center-cols-container .ag-row")).to_have_count(1)
    expect(page.locator(".dl-panel-body svg")).to_be_visible()

    panel_body = page.locator(".dl-panel-body")
    panel_body.evaluate("el => el.setAttribute('data-marker', 'untouched')")

    # A second run, started entirely through the backend -- nothing from here on ever touches the
    # page until the assertions below.
    second_run = api.create_run(models.NewRun(experiment_id=experiment_id))
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=second_run.id,
                step=0,
                metrics={"loss": 0.8},
                timestamp_utc=pendulum.now("UTC"),
            )
        ]
    )

    # The poll interval is 2s in this test session (`browser_test.py`'s `dlboard_app` fixture) --
    # give it comfortably more than one full cycle rather than racing Playwright's default 5s
    # timeout.
    expect(grid.locator(".ag-center-cols-container .ag-row")).to_have_count(2, timeout=8000)
    expect(page.locator(".dl-panel-body svg")).to_be_visible()
    assert panel_body.get_attribute("data-marker") == "untouched"
    assert console_errors == []


def test_live_update_poll_patches_only_the_chart_whose_data_changed(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Regression check for `poll_for_updates` operating at chart granularity, not panel granularity.

    Two charts sharing one panel -- a routine "Auto-generate charts" grouping -- can have very
    different update cadences (an epoch-level chart next to a per-step one); a panel-level
    live-update fingerprint would rebuild -- and visually pop -- *both* whenever *either* changed,
    since a panel's charts share one fetched dataframe. Logs an initial point for two metrics that
    land in one auto-generated panel, tags both rendered chart items, then logs new data for only
    one of them: the untouched chart's DOM node must survive exactly as it was, while the updated
    one gets a real new node.
    """
    _create_project_and_experiment(page, live_server_url, "Chart Granularity Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])

    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    # No delimiter in either name, so auto-generate collapses them into one "Ungrouped" panel with
    # two charts, alphabetically: cold_metric, hot_metric.
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=0,
                metrics={"cold_metric": 1.0, "hot_metric": 1.0},
                timestamp_utc=pendulum.now("UTC"),
            )
        ]
    )
    page.reload()
    _auto_generate_charts(page)

    chart_items = page.locator(".dl-chart-item")
    expect(chart_items).to_have_count(2)
    # An item keeps its own node and its controls; what a poll replaces is the chart in its body.
    cold_item, hot_item = chart_items.nth(0), chart_items.nth(1)
    cold_chart, hot_chart = (
        item.locator(":scope > .dl-chart-body > *").first for item in (cold_item, hot_item)
    )
    for node in (cold_item, hot_item, cold_chart, hot_chart):
        node.evaluate("el => el.setAttribute('data-marker', 'untouched')")

    # Only "hot_metric" gets new data from here on -- "cold_metric" is never touched again.
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=1,
                metrics={"hot_metric": 2.0},
                timestamp_utc=pendulum.now("UTC"),
            )
        ]
    )

    # The poll interval is 2s in this test session -- give it comfortably more than one full cycle.
    expect(hot_chart).not_to_have_attribute("data-marker", "untouched", timeout=8000)
    # The unchanged chart is not redrawn, not even by the first poll after the page loaded.
    assert cold_chart.get_attribute("data-marker") == "untouched"
    # And an item is patched in place, never a whole item nested inside another.
    for item in (cold_item, hot_item):
        assert item.get_attribute("data-marker") == "untouched"
    expect(page.locator(".dl-chart-item .dl-chart-item")).to_have_count(0)
    assert console_errors == []


def test_live_update_toggle_pauses_polling_and_persists_across_reload(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    The enable/disable switch must actually stop the poll from running -- `dcc.Interval.disabled`,
    flipped client-side, not a server-side callback that keeps firing and gets ignored -- and its
    state must survive a reload via the browser's own `localStorage` (`persistence_type="local"`),
    not the experiment's persisted `page_settings`, which every other viewer would then inherit.
    """
    _create_project_and_experiment(page, live_server_url, "Live Toggle Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])

    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=0,
                metrics={"loss": 1.0},
                timestamp_utc=pendulum.now("UTC"),
            )
        ]
    )
    page.reload()

    grid = page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID}")
    expect(grid.locator(".ag-center-cols-container .ag-row")).to_have_count(1)

    switch = page.locator(f"#{LIVE_UPDATES_ENABLED_ID}")
    live_badge = page.locator(f"#{LIVE_STATUS_ID}")
    paused_badge = page.locator(f"#{LIVE_PAUSED_BADGE_ID}")
    expect(switch).to_be_checked()
    expect(live_badge).to_be_visible()
    expect(paused_badge).not_to_be_visible()
    # `force=True` -- Mantine's `Switch` overlays a decorative track-label span that intercepts a
    # plain click before it reaches the actual (visible, enabled) `<input>` underneath.
    switch.click(force=True)
    expect(switch).not_to_be_checked()
    expect(paused_badge).to_be_visible()
    expect(live_badge).not_to_be_visible()

    # A second run, started entirely through the backend while live updates are paused.
    api.create_run(models.NewRun(experiment_id=experiment_id))

    # Comfortably longer than one full poll cycle -- nothing should change while paused.
    page.wait_for_timeout(4000)
    expect(grid.locator(".ag-center-cols-container .ag-row")).to_have_count(1)

    # The paused state survives a reload -- not reset back to the server-rendered default.
    page.reload()
    expect(page.locator(f"#{LIVE_UPDATES_ENABLED_ID}")).not_to_be_checked()
    expect(page.locator(f"#{LIVE_PAUSED_BADGE_ID}")).to_be_visible()

    # Re-enabling picks the missed update up on the next tick.
    page.locator(f"#{LIVE_UPDATES_ENABLED_ID}").click(force=True)
    expect(grid.locator(".ag-center-cols-container .ag-row")).to_have_count(2, timeout=8000)
    assert console_errors == []


def test_ctrl_k_jumps_straight_to_an_experiment_by_name(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """From any page: Ctrl+K, a few letters of an experiment's name, Enter -- and you're on it."""
    api = BasicDlboardAPI(live_server_url)
    project = api.create_project(models.NewProject(name="Jump Project", description=""))
    target = api.create_experiment(models.NewExperiment(project_id=project.id, name="needle-in-a-haystack"))

    page.goto(live_server_url)
    expect(page.get_by_role("heading", name="Projects")).to_be_visible()
    page.keyboard.press("Control+K")
    search = page.locator(f"#{JUMP_SELECT_ID}")
    expect(search).to_be_focused()
    search.press_sequentially("needle")
    page.keyboard.press("Enter")

    expect(page).to_have_url(f"{live_server_url}/experiment/{target.id}")
    expect(page.locator(f"#{PAGE_EXPERIMENT_ID}")).to_be_visible()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert console_errors == []


def test_ctrl_k_dropdown_options_are_visible_and_clickable(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Regression test: the palette's `Select` dropdown used to render inline inside the modal
    (`comboboxProps={"withinPortal": False}`), which put it inside the modal content box's own
    `overflow: auto` -- that box sizes to the search input alone, so the option list rendered below
    it was clipped and invisible even though it existed in the DOM. Defaulting to Mantine's own
    `withinPortal: True` portals the dropdown to the document body instead, outside that clipping
    box, without reintroducing the *other* failure mode a bare `Popover` has in this situation (see
    `_run_comparison_table.py`'s own Columns picker): a portalled dropdown's option is still
    click-through-able here because `Modal`'s outside-click detection (unlike `Popover`'s) already
    accounts for its own nested portals.
    """
    api = BasicDlboardAPI(live_server_url)
    project = api.create_project(models.NewProject(name="Jump Click Project", description=""))
    target = api.create_experiment(
        models.NewExperiment(project_id=project.id, name="click-target-experiment")
    )

    page.goto(live_server_url)
    expect(page.get_by_role("heading", name="Projects")).to_be_visible()
    page.keyboard.press("Control+K")
    page.locator(f"#{JUMP_SELECT_ID}").press_sequentially("click-target")
    option = page.get_by_role("option", name="click-target-experiment")
    expect(option).to_be_in_viewport()
    option.click()

    expect(page).to_have_url(f"{live_server_url}/experiment/{target.id}")
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert console_errors == []


def test_a_copied_chart_link_opens_that_chart_without_changing_the_shared_layout(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Copy a link to a chart in the first (open) panel, close that panel, and follow the link: the
    chart's panel opens for this visit and the chart is scrolled to and highlighted -- but the
    experiment's saved layout (the panel still closed, for everyone else) is untouched.
    """
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    _create_project_and_experiment(page, live_server_url, "Deep Link Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=step,
                metrics={"train/loss": 1.0 / (step + 1), "val/loss": 2.0 / (step + 1)},
                timestamp_utc=pendulum.now("UTC"),
            )
            for step in range(3)
        ]
    )
    page.reload()
    _auto_generate_charts(page)
    # Auto-generating on the shared page branches into a view of your own (see `save_page`) --
    # wait for that branch's `?view=` to land before copying a link off `page.url`, or the copied
    # link (and the "close its panel"/`_open_panels` checks below, which also have to follow the
    # branch rather than the now-untouched shared page) would still be reading the page it just
    # branched *away* from.
    expect(page).to_have_url(re.compile(r"\?view=\d+$"))

    chart = page.locator(".dl-chart-item").first
    chart.hover()
    chart.get_by_role("button", name="Copy a link to this chart").click()
    link = page.evaluate("navigator.clipboard.readText()")
    chart_id = chart.get_attribute("data-chart-id")
    assert link == f"{page.url}&chart={chart_id}"

    page.get_by_role("button", name="train", exact=True).click()  # close its panel
    view_id = int(parse_qs(urlparse(page.url).query)["view"][0])
    expect(page).to_have_url(re.compile(r"[?&]open=%5B%5D"))  # closed, in the URL -- not the view

    page.goto(link)
    linked = page.locator(f'[data-chart-id="{chart_id}"]')
    expect(linked).to_be_in_viewport()
    expect(linked).to_have_class(re.compile("dl-chart-focus"))
    view = get_system_data_store().get_view(BasicExperimentPage, view_id)
    assert view is not None
    assert {"open_panel", "active_tab"}.isdisjoint(view.page_settings)  # browsing is never saved to a view
    assert console_errors == []


def test_changes_in_a_saved_view_leave_the_shared_view_alone(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Save the page as a personal view, change it there (a panel's layout), and the shared page --
    what everyone else sees -- keeps its own layout; switching back shows exactly that.
    """
    _create_project_and_experiment(page, live_server_url, "Views Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    # Seeded directly on the shared page, bypassing the UI -- going through "Auto-generate charts"
    # instead would itself already branch into a view of the saving user's own (`save_page` forks
    # on *every* structural edit, including this one), leaving the shared page with nothing to
    # keep for this test to actually prove.
    store = get_system_data_store()
    shared = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(shared.model_copy(update={"panels": [PanelInstance[Any, Any](name="Losses")]}))
    page.reload()
    expect(page.get_by_role("textbox", name="View", exact=True)).to_have_value("Shared view")

    page.get_by_role("button", name="View actions").click()
    page.get_by_role("menuitem", name="Duplicate this view…").click()
    page.get_by_role("textbox", name="View name").fill("my layout")
    page.get_by_role("button", name="Duplicate").click()
    # Not just "some `?view=`" -- the bare experiment URL (no `?view=` at all, where this started)
    # would never match that regex anyway, but wait for the *navigation itself* first so the two
    # checks below aren't just reading the empty content between actions.
    expect(page).to_have_url(re.compile(r"\?view=\d+$"))
    view_id = int(page.url.rsplit("=", 1)[-1])
    expect(page.get_by_role("textbox", name="View", exact=True)).to_have_value("my layout")

    _open_panel_menu(page)
    page.get_by_text("Grid", exact=True).first.click()
    _wait_until(
        lambda: [
            p.layout
            # pyrefly: ignore [missing-attribute]
            for p in store.get_view(BasicExperimentPage, view_id).panels
        ][:1],
        ["grid"],
    )
    # A grid panel's column count is its own setting too, saved with the view and surviving a reload.
    _open_panel_menu(page)
    columns = page.get_by_role("textbox", name="Grid columns")
    expect(columns).to_have_value("3")
    columns.fill("2")
    columns.press("Enter")
    _wait_until(
        lambda: [
            p.grid_columns
            # pyrefly: ignore [missing-attribute]
            for p in store.get_view(BasicExperimentPage, view_id).panels
        ][:1],
        [2],
    )
    page.reload()
    _open_panel_menu(page)
    expect(page.get_by_role("textbox", name="Grid columns")).to_have_value("2")
    shared = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    assert {(p.layout, p.grid_columns) for p in shared.panels} == {("packed", 3)}

    page.get_by_role("textbox", name="View", exact=True).click()
    page.get_by_role("option", name="Shared view").click()
    expect(page).to_have_url(f"{live_server_url}/experiment/{experiment_id}")
    expect(page.get_by_role("textbox", name="View", exact=True)).to_have_value("Shared view")
    assert console_errors == []


def test_editing_the_shared_page_branches_into_your_own_view_without_asking(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    The whole point: deleting a panel while looking at the shared page -- no "save as a view" step
    first -- must not delete it for everyone. It should silently branch into a view of your own
    (landing on `?view=<id>` with no page reload) and delete it there instead.
    """
    _create_project_and_experiment(page, live_server_url, "Branch On Edit Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=0,
                metrics={"loss": 1.0},
                timestamp_utc=pendulum.now("UTC"),
            )
        ]
    )
    # Seeded directly on the shared page, bypassing the UI, so deleting it below is the *first*
    # edit anyone's made -- auto-generating it through the UI first would itself already have
    # branched into a view of the deleting user's own (`save_page` forks on *every* structural
    # edit, not just this one), leaving nothing here to prove.
    store = get_system_data_store()
    shared = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(shared.model_copy(update={"panels": [PanelInstance[Any, Any](name="Losses")]}))
    page.reload()
    expect(page.get_by_role("textbox", name="View", exact=True)).to_have_value("Shared view")
    # Not yours, so the header says up front that an edit will land in a copy.
    hint = page.get_by_text("Edits save to a copy")
    expect(hint).to_be_visible()

    _open_panel_menu(page)
    page.get_by_role("menuitem", name="Delete panel…").click()
    page.get_by_role("button", name="Delete", exact=True).click()

    expect(page).to_have_url(re.compile(r"\?view=\d+$"))
    view_id = int(page.url.rsplit("=", 1)[-1])
    expect(page.get_by_role("textbox", name="View", exact=True)).to_have_value("My view")
    # ...and says what just happened, instead of leaving a new view to be noticed in the picker.
    expect(page.get_by_text("Saved to your own view")).to_be_visible()
    expect(hint).to_be_hidden()
    store = get_system_data_store()
    branched = store.get_view(BasicExperimentPage, view_id)
    assert branched is not None
    assert branched.panels == []

    page.goto(f"{live_server_url}/experiment/{experiment_id}")
    expect(page.locator(".dl-panel-item-header")).to_be_visible()
    shared = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    assert [p.name for p in shared.panels] == ["Losses"]
    assert console_errors == []


def test_editing_a_view_you_do_not_own_branches_into_a_separate_view_of_your_own(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """Opening someone else's view from a link and editing it must not change what they see either."""
    _create_project_and_experiment(page, live_server_url, "Foreign View Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    store = get_system_data_store()
    alice = store.get_or_create_user(models.Principal.unverified("alice-owns-this-view"))
    alices_view = store.create_view(
        BasicExperimentPage,
        models.NewPage[Any, Any](
            experiment_id=experiment_id,
            owner_id=alice.id,
            name="alice's view",
            panels=[PanelInstance[Any, Any](name="kept")],
        ),
    )

    page.goto(f"{live_server_url}/experiment/{experiment_id}?view={alices_view.id}")
    expect(page.get_by_role("textbox", name="View", exact=True)).to_have_value("alice's view")
    # Not the owner: no rename/delete for a view that isn't yours.
    expect(page.get_by_role("button", name="View actions")).to_be_visible()
    page.get_by_role("button", name="View actions").click()
    expect(page.get_by_role("menuitem", name="Duplicate this view…")).to_be_visible()
    expect(page.get_by_role("menuitem", name="Rename this view…")).to_have_count(0)
    expect(page.get_by_role("menuitem", name="Delete this view…")).to_have_count(0)
    page.keyboard.press("Escape")

    _open_panel_menu(page)
    page.get_by_role("menuitem", name="Rename panel…").click()
    page.get_by_role("textbox", name="Panel name", exact=True).fill("renamed by someone else")
    page.get_by_role("button", name="Save", exact=True).click()

    # Not just "some `?view=`" -- alice's own id already satisfies that trivially, so this has to
    # wait for it to actually become a *different* one (the branch), which is the whole point.
    expect(page).not_to_have_url(f"{live_server_url}/experiment/{experiment_id}?view={alices_view.id}")
    expect(page).to_have_url(re.compile(r"\?view=\d+$"))
    my_view_id = int(page.url.rsplit("=", 1)[-1])
    assert my_view_id != alices_view.id
    mine = store.get_view(BasicExperimentPage, my_view_id)
    assert mine is not None
    assert mine.owner_id != alice.id
    assert [p.name for p in mine.panels] == ["renamed by someone else"]
    untouched = store.get_view(BasicExperimentPage, alices_view.id)
    assert untouched is not None
    assert [p.name for p in untouched.panels] == ["kept"]
    assert console_errors == []


def test_renaming_a_view_you_own_updates_the_picker(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    _create_project_and_experiment(page, live_server_url, "Rename View Experiment")
    page.locator(".experiment-card").click()

    page.get_by_role("button", name="View actions").click()
    page.get_by_role("menuitem", name="Duplicate this view…").click()
    page.get_by_role("textbox", name="View name").fill("first name")
    page.get_by_role("button", name="Duplicate").click()
    expect(page.get_by_role("textbox", name="View", exact=True)).to_have_value("first name")

    page.get_by_role("button", name="View actions").click()
    page.get_by_role("menuitem", name="Rename this view…").click()
    page.get_by_role("textbox", name="View name").fill("better name")
    page.get_by_role("button", name="Save name").click()

    expect(page.get_by_role("textbox", name="View", exact=True)).to_have_value("better name")
    assert console_errors == []


def test_a_view_branched_by_an_edit_can_be_deleted_without_a_reload(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Most views are never saved on purpose -- an edit on the shared page branches into one (see
    `save_page`). The header isn't rebuilt when that happens, so its delete item has to appear in
    place, not only after a reload.
    """
    _create_project_and_experiment(page, live_server_url, "Delete Branched View Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    store = get_system_data_store()
    shared = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(shared.model_copy(update={"panels": [PanelInstance[Any, Any](name="Losses")]}))
    page.reload()

    _open_panel_menu(page)
    page.get_by_role("menuitem", name="Delete panel…").click()
    page.get_by_role("button", name="Delete", exact=True).click()
    expect(page).to_have_url(re.compile(r"\?view=\d+$"))
    view_id = int(page.url.rsplit("=", 1)[-1])

    page.get_by_role("button", name="View actions").click()
    page.get_by_role("menuitem", name="Delete this view…").click()
    page.get_by_role("button", name="Delete", exact=True).click()

    expect(page).to_have_url(re.compile(rf"/experiment/{experiment_id}\??$"))
    expect(page.get_by_role("textbox", name="View", exact=True)).to_have_value("Shared view")
    assert store.get_view(BasicExperimentPage, view_id) is None
    assert console_errors == []


def test_notes_post_to_the_thread_and_arrive_live_from_others(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """Post a note about a run; then someone else's note shows up in the open thread without a reload."""
    _create_project_and_experiment(page, live_server_url, "Notes Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    api.create_run(models.NewRun(experiment_id=experiment_id, name="baseline-run"))
    page.reload()

    page.get_by_role("button", name=re.compile("^Notes")).click()
    drawer = page.get_by_role("dialog", name="Notes")
    # Scoped to the thread itself, not the whole drawer -- the drawer also holds the compose
    # textarea, which still holds this same text for a moment after posting (until `post_note`'s
    # own response clears it), and a bare `drawer.get_by_text(...)` would match both.
    thread = drawer.locator(f"#{NOTES_THREAD_ID}")
    expect(thread.get_by_text("No notes yet")).to_be_visible()
    drawer.get_by_role("textbox", name="Note").fill("baseline diverges after step 40")
    drawer.get_by_placeholder("About runs…").click()
    page.get_by_role("option", name="baseline-run").click()
    drawer.get_by_role("button", name="Post").click()

    expect(thread.get_by_text("baseline diverges after step 40")).to_be_visible()
    expect(
        thread.locator(".mantine-Badge-root", has=page.locator(".dl-swatch"), has_text="baseline-run")
    ).to_be_visible()
    expect(page.locator(f"#{NOTES_COUNT_ID}")).to_have_text("1")

    store = get_system_data_store()
    colleague = store.get_or_create_user(models.Principal.unverified("colleague"))
    store.add_comment(models.NewComment(experiment_id=experiment_id, author_id=colleague.id, body="agreed"))
    expect(thread.get_by_text("agreed")).to_be_visible(timeout=10_000)  # one live-poll tick away
    expect(page.locator(f"#{NOTES_COUNT_ID}")).to_have_text("2")
    assert console_errors == []


def test_run_compare_modal_diffs_runs_and_its_state_is_a_shareable_link(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    The compare modal opens from the navbar, diffs the runs' hyperparameters and latest metrics, and
    mirrors its state (runs, mode, search) into the URL -- which, loaded fresh, reopens the same view.
    Only a browser sees the clientside URL sync and the cell highlighting, and the modal opening
    already filled in from a link.
    """
    _create_project_and_experiment(page, live_server_url, "Compare Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])

    api = BasicDlboardAPI(live_server_url)
    for name, optimizer, loss in (("run-a", "adam", 0.5), ("run-b", "sgd", 0.4)):
        run = api.create_run(models.NewRun(experiment_id=experiment_id, name=name))
        api.log_hyperparams(
            models.NewHyperParams.from_raw(run.id, experiment_id, {"lr": 0.1, "optimizer": optimizer})
        )
        api.log_metric_batch(
            [
                models.LoggedMetrics(
                    experiment_id=experiment_id,
                    run_id=run.id,
                    step=0,
                    metrics={"loss": loss},
                    timestamp_utc=pendulum.now("UTC"),
                )
            ]
        )
    page.reload()

    page.locator(f"#{COMPARE_OPEN_ID}").click()
    grid = page.locator(f"#{COMPARE_GRID_ID}")
    rows = grid.locator(".ag-center-cols-container .ag-row")
    # Pre-filled with the charted runs (newest first, so run-b is the baseline); only differing keys are listed, with the other run's cell flagged.
    expect(rows).to_have_count(2)
    expect(grid.get_by_role("gridcell", name="adam")).to_be_visible()
    expect(grid.locator(".dl-compare-changed", has_text="adam")).to_be_visible()
    expect(page).to_have_url(re.compile(r"compare=\d+%2C\d+"))

    page.locator(f"#{COMPARE_MODE_ID}").get_by_text("All").click()
    expect(rows).to_have_count(3)
    page.locator(f"#{COMPARE_SEARCH_ID}").fill("opt")
    expect(rows).to_have_count(1)
    expect(page).to_have_url(re.compile(r"compare_mode=all.*compare_q=opt"))
    link = page.url

    page.goto(link)
    expect(page.get_by_role("dialog", name="Compare runs")).to_be_visible()
    expect(rows).to_have_count(1)
    expect(page.locator(f"#{COMPARE_SEARCH_ID}")).to_have_value("opt")

    page.keyboard.press("Escape")
    expect(page).not_to_have_url(re.compile(r"compare"))
    assert console_errors == []


def test_changing_a_grids_column_count_restyles_it_without_rebuilding_its_charts(
    page: Page, live_server_url: str
) -> None:
    """
    The count is only CSS, so the browser applies it itself and the server just saves it.

    It used to rebuild every chart in the panel for each click of the counter -- on a panel with
    hundreds of charts that blanked the page. A chart's DOM node surviving the change is what tells
    "restyled in place" from "torn down and rebuilt".
    """
    _create_project_and_experiment(page, live_server_url, "Grid Columns Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=step,
                metrics={"loss": 1.0 - step * 0.1},
                timestamp_utc=pendulum.now("UTC"),
            )
            for step in range(5)
        ]
    )
    page.reload()
    _auto_generate_charts(page)
    _open_panel_menu(page)
    page.get_by_text("Grid", exact=True).first.click()
    # Switching layout rebuilds the panel, which closes its menu.
    expect(page.locator(".dl-panel-body .mantine-SimpleGrid-root").first).to_be_visible()
    _open_panel_menu(page)
    columns = page.get_by_role("textbox", name="Grid columns")
    expect(columns).to_be_visible()
    # Marked once the panel is a grid (switching layout rebuilds it): only a rebuild can lose this.
    chart = page.locator(".dl-chart-item").first
    expect(chart).to_be_visible()
    chart.evaluate("el => { el.dataset.survives = 'yes' }")
    columns.fill("2")
    columns.press("Enter")

    grid = page.locator(".dl-panel-body .mantine-SimpleGrid-root").first
    expect(grid).to_have_css("grid-template-columns", re.compile(r"^\S+ \S+$"))
    expect(chart).to_have_attribute("data-survives", "yes")
    page.reload()
    _open_panel_menu(page)
    expect(page.get_by_role("textbox", name="Grid columns")).to_have_value("2")


def test_auto_generate_leaves_a_huge_panel_closed_and_opens_a_small_one(
    page: Page, live_server_url: str
) -> None:
    """
    Mounting a panel costs the browser about 40 ms per chart, so one of a hundred charts used to make
    the whole page wait for it right after "Create charts". It stays closed now, with its chart count
    on its header, and the first panel small enough to render at once is opened instead.
    """
    _create_project_and_experiment(page, live_server_url, "Big Panel Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    many = {f"big/m{i}": 1.0 for i in range(AUTO_OPEN_MAX_CHARTS + 1)}
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=step,
                metrics={**many, "small/x": 1.0},
                timestamp_utc=pendulum.now("UTC"),
            )
            for step in range(3)
        ]
    )
    page.reload()
    _auto_generate_charts(page)

    expect(page.get_by_text(f"{AUTO_OPEN_MAX_CHARTS + 1} charts", exact=True)).to_be_visible()
    expect(page.locator(".dl-panel-body .recharts-surface")).to_have_count(1)


def test_suggest_charts_filters_selects_and_adds_many_at_once(page: Page, live_server_url: str) -> None:
    """
    Suggestions used to be one add button per key, which is a hundred clicks (and a hundred page
    rebuilds) on an experiment with a hundred un-charted metrics. A filter narrows the list, "Select
    all" ticks what it shows, and one button adds everything ticked in a single update.
    """
    _create_project_and_experiment(page, live_server_url, "Suggest Many Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=step,
                metrics={"train/loss": 1.0, "train/acc": 0.5, "val/loss": 0.9},
                timestamp_utc=pendulum.now("UTC"),
            )
            for step in range(3)
        ]
    )
    page.reload()
    page.locator(f"#{NEW_PANEL_OPEN_ID}").click()
    page.locator(f"#{NEW_PANEL_NAME_ID}").fill("Scratch")
    page.locator(f"#{NEW_PANEL_ID}").click()
    page.get_by_role("button", name="Suggest charts").first.click()
    for key in ("train/loss", "train/acc", "val/loss"):
        expect(page.get_by_text(key, exact=True)).to_be_visible()

    page.get_by_placeholder("Filter by name or panel").fill("train/")
    expect(page.get_by_text("val/loss", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Select all").click()
    page.get_by_role("button", name="Add selected (2)").click()

    # Both charts went into the "train" panel in the one update (adding closes the drawer, and opening
    # it again starts unfiltered); what is left to suggest is just the one that was filtered out.
    expect(page.get_by_text("2 charts", exact=True)).to_be_visible()
    page.get_by_role("button", name="Suggest charts").first.click()
    expect(page.get_by_text("val/loss", exact=True)).to_be_visible()
    expect(page.get_by_text("train/loss", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Add selected (0)")).to_be_disabled()


def test_the_runs_table_shows_how_each_run_ended_and_for_how_long(page: Page, live_server_url: str) -> None:
    """A crashed run used to look exactly like a finished one, and nothing said how long either took."""
    _create_project_and_experiment(page, live_server_url, "Run Status Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    still_running = api.create_run(models.NewRun(experiment_id=experiment_id, name="still-going"))
    crashed = api.create_run(models.NewRun(experiment_id=experiment_id, name="crashed"))
    done = api.create_run(models.NewRun(experiment_id=experiment_id, name="all-done"))
    walked_away = api.create_run(models.NewRun(experiment_id=experiment_id, name="walked-away"))
    store = get_system_data_store()
    store.finish_run(
        crashed.id,
        models.RunStatus.FAILED,
        crashed.created_at + pendulum.duration(hours=1, minutes=2, seconds=5),
    )
    store.finish_run(
        done.id, models.RunStatus.FINISHED, done.created_at + pendulum.duration(minutes=3, seconds=23)
    )
    store.finish_run(
        walked_away.id, models.RunStatus.UNKNOWN, walked_away.created_at + pendulum.duration(days=2, hours=3)
    )
    assert still_running.status == models.RunStatus.RUNNING

    page.reload()

    rows = page.locator(".dl-run-table .ag-row")
    expect(rows.filter(has_text="still-going")).to_contain_text("running")
    expect(rows.filter(has_text="crashed")).to_contain_text("✕ 1h 2m")
    expect(rows.filter(has_text="all-done")).to_contain_text("3m 23s")
    expect(rows.filter(has_text="walked-away")).to_contain_text("? 2d 3h")


def test_panel_menu_controls_mount_only_when_the_menu_opens(page: Page, live_server_url: str) -> None:
    """
    A header's less-used controls cost the browser ~50 ms each to mount, which made a page of hundreds
    of panels take seconds to appear -- so they live in a menu whose contents only exist once it opens.
    """
    _create_project_and_experiment(page, live_server_url, "Lazy Panel Menu Experiment")
    page.locator(".experiment-card").click()
    _create_panel(page, "only")

    sync_switch = page.get_by_role("switch", name="Sync crosshair and step across charts")
    expect(sync_switch).to_have_count(0)
    _open_panel_menu(page)
    expect(sync_switch).to_have_count(1)


def test_a_page_from_an_older_version_offers_a_reload(page: Page, live_server_url: str) -> None:
    """
    A tab left open across a deploy sends callbacks in their old shape; the server answers with a 409
    and the page shows one "Reload" prompt, which a Python-level test can't see the banner side of.
    Rewrites Dash's own request into an old shape, so the 409 comes from the real server.
    """

    def with_an_extra_input(route: Route) -> None:
        body = cast("dict[str, Any]", route.request.post_data_json)
        route.continue_(post_data=json.dumps({**body, "inputs": [*body.get("inputs", []), {}]}))

    callbacks = re.compile(r"/_dash-update-component")  # Dash adds a query string, so a bare glob misses it
    page.route(callbacks, with_an_extra_input)
    page.goto(live_server_url)

    banner = page.locator("#dl-stale-page")
    expect(banner).to_contain_text("dlboard was updated since this page loaded")
    page.unroute(callbacks)
    banner.get_by_role("button", name="Reload").click()
    expect(banner).to_have_count(0)


def test_checkpoints_are_one_tabbed_list_with_download_links(page: Page, live_server_url: str) -> None:
    """
    Auto-generate gives a directory of checkpoint files (one key apiece) a single list, split into a
    tab per kind with a link to each file -- AG Grid's own rendering, which only a browser shows.
    """
    _create_project_and_experiment(page, live_server_url, "Checkpoint List Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])

    store = get_system_data_store()
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_artifact_refs(
        [
            models.Artifact(
                key=f"checkpoints/{name}.ckpt",
                fname=f"{name}.ckpt",
                run_id=run.id,
                experiment_id=experiment_id,
                step=step,
                ref=f"file:///{name}.ckpt",
                tags={models.FILE_KIND_TAG: "checkpoint", "tag": tag},
            )
            for step, (name, tag) in enumerate(
                [("last", "latest"), ("epoch-1", "best"), ("epoch-2", "best_k")]
            )
        ]
    )
    page.reload()
    _auto_generate_charts(page)

    expect(page.get_by_role("tab", name=re.compile("^Latest"))).to_be_visible()
    page.get_by_role("tab", name=re.compile("^Best")).click()
    link = page.locator(".dl-file-list .ag-cell a")
    expect(link).to_have_text("epoch-1.ckpt")
    expect(link).to_have_attribute("href", re.compile(r"^/artifact/\d+"))


def test_panels_are_paged_and_filtered_and_the_page_is_a_shareable_link(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    An experiment with many panels shows one page of them, narrowed by a fuzzy name filter and sized by
    the viewer. Which page and filter is in the URL, so the link reproduces it; the page size is the
    viewer's own, and survives a reload without being in the link.
    """
    _create_project_and_experiment(page, live_server_url, "Paged Panels Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=step,
                metrics={f"p{i:02}/loss": 1.0 for i in range(13)},
                timestamp_utc=pendulum.now("UTC"),
            )
            for step in range(2)
        ]
    )
    page.reload()
    _auto_generate_charts(page)

    headers = page.locator(".dl-panel-item-header")
    area = page.locator(f"#{PANEL_AREA_ID}")
    expect(headers).to_have_count(10)
    expect(page).not_to_have_url(re.compile(r"panels_page"))

    area.get_by_role("button", name="2", exact=True).first.click()
    expect(headers).to_have_count(3)
    expect(headers.first).to_contain_text("p10")
    expect(page).to_have_url(re.compile(r"panels_page=2"))

    page.locator(f"#{PANEL_FILTER_ID}").fill("p07")
    expect(headers).to_have_count(1)
    expect(headers.first).to_contain_text("p07")
    expect(page).to_have_url(re.compile(r"panels_q=p07"))
    expect(page).not_to_have_url(re.compile(r"panels_page"))

    link = page.url
    page.goto(link)
    expect(headers).to_have_count(1)
    expect(page.locator(f"#{PANEL_FILTER_ID}")).to_have_value("p07")

    page.locator(f"#{PANEL_FILTER_ID}").fill("")
    expect(headers).to_have_count(10)
    # The page-size choice is a bare number at the bottom, in line with and to the right of the pager.
    size_select = page.locator(f"#{PANEL_PAGE_SIZE_ID}")
    size_box = size_select.bounding_box()
    pager_box = area.locator(".mantine-Pagination-root").bounding_box()
    last_header_box = headers.last.bounding_box()
    assert size_box is not None
    assert pager_box is not None
    assert last_header_box is not None
    assert abs(size_box["y"] - pager_box["y"]) < 20
    assert size_box["x"] > pager_box["x"] + pager_box["width"]
    assert size_box["y"] > last_header_box["y"]
    size_select.click()
    page.get_by_role("option", name="5", exact=True).click()
    expect(headers).to_have_count(5)
    expect(page).not_to_have_url(re.compile(r"sizes|per_page"))
    page.reload()
    expect(headers).to_have_count(5)
    assert console_errors == []


def test_a_panels_charts_are_paged_and_filtered_by_metric_name(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    A panel with many charts mounts one page of them, narrowed by a fuzzy filter on the metric names the
    charts show. Both the page and the filter are in the URL, and a reload lands on the same charts.
    """
    _create_project_and_experiment(page, live_server_url, "Paged Charts Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=step,
                metrics={f"g/m{i:02}": float(i) for i in range(14)},
                timestamp_utc=pendulum.now("UTC"),
            )
            for step in range(2)
        ]
    )
    page.reload()
    _auto_generate_charts(page)

    charts = page.locator(".dl-chart-item")
    panel = page.locator(".mantine-Accordion-panel")
    chart_filter = page.get_by_placeholder("Filter charts")
    expect(charts).to_have_count(0)  # more than AUTO_OPEN_MAX_CHARTS, so it starts closed
    page.locator(".dl-panel-item-header", has_text="g").first.click()
    expect(charts).to_have_count(12)
    expect(page).not_to_have_url(re.compile(r"charts="))

    # Nothing sits above the charts: the filter is in the panel's header, and the pager (centered) is
    # under the last chart.
    header_box = page.locator(".dl-panel-item-header").first.bounding_box()
    filter_box = chart_filter.bounding_box()
    panel_box = panel.bounding_box()
    pager = panel.locator(".mantine-Pagination-root")
    pager_box = pager.bounding_box()
    last_chart_box = charts.last.bounding_box()
    assert header_box is not None
    assert filter_box is not None
    assert panel_box is not None
    assert pager_box is not None
    assert last_chart_box is not None
    assert header_box["y"] <= filter_box["y"] <= header_box["y"] + header_box["height"]
    assert pager_box["y"] >= last_chart_box["y"] + last_chart_box["height"] - 1
    assert abs((pager_box["x"] + pager_box["width"] / 2) - (panel_box["x"] + panel_box["width"] / 2)) < 30

    # Flipping from the bottom of a long page takes you back to the top of it.
    pager.scroll_into_view_if_needed()
    expect(page.locator(".dl-panel-item-header").first).not_to_be_in_viewport()
    panel.get_by_role("button", name="2", exact=True).click()
    expect(page.locator(".dl-panel-item-header").first).to_be_in_viewport()
    expect(charts).to_have_count(2)
    expect(page).to_have_url(re.compile(r"charts="))

    chart_filter.fill("m03")
    expect(charts).to_have_count(1)
    expect(charts.first).to_contain_text("m03")

    page.reload()
    expect(charts).to_have_count(1)
    expect(chart_filter).to_have_value("m03")

    chart_filter.fill("")
    expect(charts).to_have_count(12)
    assert console_errors == []


def test_an_image_charts_per_tag_display_is_edited_as_table_rows_in_the_chart_editor(
    page: Page, live_server_url: str, tmp_path: Path
) -> None:
    """A unit tag shows in the largest unit that fits, until the chart's editor maps that tag to another."""
    _create_project_and_experiment(page, live_server_url, "Tag Display Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    api.log_artifact_batch(
        [
            Image(
                key="samples/a",
                image=np.zeros((8, 8, 3), dtype=np.uint8),
                step=0,
                tags={"took": models.Quantity(value=5400, unit=models.TimeUnit.SECONDS)},
            ).to_artifact(tmp_path, run.id, experiment_id)
        ]
    )
    _wait_for_artifacts(experiment_id, 1)
    page.reload()
    _auto_generate_charts(page)

    chart = page.locator(".dl-chart-item")
    expect(chart).to_contain_text("took: 1.500 h")
    chart.hover()
    chart.get_by_role("button", name="Edit chart").click()

    # A row per tag in a grid, not JSON: add one, name the tag, and give it a precision.
    # The chart has another table now (its `layers`), with buttons of its own: this is the tag display's.
    editor = page.get_by_role("complementary", name="Chart settings")
    editor.get_by_role("button", name="Tags").click()  # the settings are in collapsed sections
    add_row = page.locator('button[id*="tag_display"][id*="table-add"]')
    rows = editor.locator(".ag-center-cols-container .ag-row")
    add_row.click()  # opens the new row's name for typing; clicking away leaves it blank
    editor.get_by_text("Chart type").click()  # away from the cell, which commits its edit
    expect(rows).to_have_count(1)
    rows.first.locator(".ag-checkbox-input-wrapper").click()
    page.locator('button[id*="tag_display"][id*="table-remove"]').click()
    expect(rows).to_have_count(0)
    add_row.click()  # the new row's name opens for typing, a moment after the click
    expect(editor.locator('.ag-cell[col-id="key"] input')).to_be_focused()
    page.keyboard.type("took")
    page.keyboard.press("Tab")
    page.keyboard.type("2")
    page.keyboard.press("Enter")
    page.locator(f"#{CHART_EDITOR_SAVE_ID}").click()

    expect(chart).to_contain_text("took: 1.5 h")


def test_image_series_charts_are_titled_fill_their_chart_and_share_a_synced_panels_step(
    page: Page, live_server_url: str, tmp_path: Path
) -> None:
    """
    An image chart is titled with its artifact key, and one run's image fills the chart's width rather
    than a third of it. Its step slider moves the other image charts' in a panel that syncs, each to
    the latest step it has, and leaves them alone once the panel's sync is switched off.
    """
    _create_project_and_experiment(page, live_server_url, "Image Series Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    wide = np.zeros((400, 1600, 3), dtype=np.uint8)
    api.log_artifact_batch(
        [
            Image(key=key, image=wide, step=step).to_artifact(tmp_path, run.id, experiment_id)
            for key, steps in {"samples/a": (0, 10), "samples/b": (0, 5)}.items()
            for step in steps
        ]
    )
    _wait_for_artifacts(experiment_id, 4)
    page.reload()
    _auto_generate_charts(page)

    charts = page.locator(".dl-chart-item")
    expect(charts).to_have_count(2)
    expect(charts.get_by_text("samples/a", exact=True)).to_be_visible()
    expect(charts.get_by_text("samples/b", exact=True)).to_be_visible()
    first_image_box = charts.first.locator("img").bounding_box()
    assert first_image_box is not None
    assert first_image_box["width"] > 600  # a lone run's image spans the chart, not a third of it

    thumbs = charts.get_by_role("slider")
    expect(thumbs).to_have_count(2)
    thumbs.nth(0).focus()
    page.keyboard.press("ArrowRight")
    expect(thumbs.nth(0)).to_have_attribute("aria-valuenow", "10")
    expect(thumbs.nth(1)).to_have_attribute("aria-valuenow", "5")  # its latest step, not a step it lacks

    _open_panel_menu(page)
    page.get_by_role("switch", name="Sync crosshair and step across charts").click()
    expect(page.locator("[data-dl-panel-sync]")).to_have_count(0)  # the panel is rebuilt, unsynced
    thumbs.nth(0).focus()
    page.keyboard.press("ArrowLeft")
    expect(thumbs.nth(0)).to_have_attribute("aria-valuenow", "0")
    expect(thumbs.nth(1)).to_have_attribute("aria-valuenow", "5")


def test_a_scrubbed_thumbnail_shows_a_spinner_until_its_new_step_has_loaded(
    page: Page, live_server_url: str, tmp_path: Path
) -> None:
    """
    Moving an image chart's slider to a step whose picture is still downloading dims the old picture under a
    spinner rather than presenting it as that step's, and the spinner goes once the new picture has arrived.
    """
    _create_project_and_experiment(page, live_server_url, "Image Loader Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    picture = np.zeros((32, 32, 3), dtype=np.uint8)
    api.log_artifact_batch(
        [
            Image(key="samples/a", image=picture, step=step).to_artifact(tmp_path, run.id, experiment_id)
            for step in (0, 10)
        ]
    )
    _wait_for_artifacts(experiment_id, 2)
    page.reload()
    _auto_generate_charts(page)

    thumb = page.locator(".dl-image-series-thumb")
    expect(thumb).to_have_count(1)
    page.wait_for_function(
        "() => [...document.querySelectorAll('.dl-image-series-thumb img')].every((img) => img.complete)"
    )

    # Hold back every picture the thumbnail asks for from here on: the one for step 10 stays in flight.
    held: list[Route] = []

    def hold(route: Route) -> None:
        held.append(route)

    page.route(re.compile(r"/artifact/\d+"), hold)
    page.get_by_role("slider").focus()
    page.keyboard.press("ArrowRight")
    expect(thumb).to_have_attribute("data-dl-loading", "")
    # The spinner is the thumbnail's `::after`, animated only by the chart's stylesheet.
    assert thumb.evaluate("el => getComputedStyle(el, '::after').animationName") == "dl-image-series-spin"
    for route in held:
        route.continue_()
    expect(thumb).not_to_have_attribute("data-dl-loading", "")


def test_an_edited_image_chart_still_scrubs_and_opens_its_enlarged_view(
    page: Page, live_server_url: str, tmp_path: Path
) -> None:
    """
    The editor's preview renders the chart under the very ids the saved chart gets. Left in the page, it made Dash
    unable to resolve the saved chart's callbacks, so its slider and enlarged view died until the page was reloaded.
    """
    _create_project_and_experiment(page, live_server_url, "Edited Image Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    api.log_artifact_batch(
        [
            Image(key="samples/a", image=np.zeros((8, 8, 3), dtype=np.uint8), step=step).to_artifact(
                tmp_path, run.id, experiment_id
            )
            for step in (0, 10)
        ]
    )
    _wait_for_artifacts(experiment_id, 2)
    page.reload()
    _auto_generate_charts(page)

    chart = page.locator(".dl-chart-item")
    chart.hover()
    chart_id = chart.get_attribute("data-chart-id")
    chart.get_by_role("button", name="Edit chart").click()
    page.locator(f"#{CHART_EDITOR_SAVE_ID}").click()
    expect(page.get_by_role("complementary", name="Chart settings")).to_be_hidden()
    expect(chart).to_have_attribute(
        "data-chart-id", chart_id or ""
    )  # an edit keeps the chart, and so its links

    picture = chart.locator(".dl-image-series-thumb img")
    before = picture.get_attribute("src")
    page.get_by_role("slider").focus()
    page.keyboard.press("ArrowRight")
    expect(picture).not_to_have_attribute("src", before or "")
    chart.locator(".dl-image-series-thumb").click()
    expect(page.get_by_role("dialog")).to_be_visible()


def _log_masks_over_a_picture(
    live_server_url: str, tmp_path: Path, experiment_id: int, *, extra_layer: bool = False
) -> None:
    """A picture with a `truth` and a `prediction` overlay, and (if asked) a mask image under a key of its own."""
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    labels = np.zeros((1, 48, 64), dtype=np.int64)
    labels[:, 10:30, 10:40] = 1
    mask = rle.run_length_encode(labels, colors={1: "#ff0000"}, classnames={1: "cat"})
    artifacts = [
        Image(
            key="samples/a", image=np.full((48, 64, 3), 128, dtype=np.uint8), step=0, format="jpg"
        ).to_artifact(tmp_path, run.id, experiment_id),
        Overlay.over("samples/a", name="truth", shapes=[mask], step=0).to_artifact(
            tmp_path, run.id, experiment_id
        ),
        Overlay.over("samples/a", name="prediction", shapes=[mask], step=0).to_artifact(
            tmp_path, run.id, experiment_id
        ),
    ]
    if extra_layer:
        extra = np.zeros((12, 16, 3), dtype=np.uint8)
        extra[3:9, 4:12] = (0, 255, 0)
        artifacts.append(
            Image(key="samples/mask", image=extra, step=0).to_artifact(tmp_path, run.id, experiment_id)
        )
    api.log_artifact_batch(artifacts)
    _wait_for_artifacts(experiment_id, len(artifacts))


def _open_layers_editor(page: Page, chart: Locator) -> Locator:
    """Open the chart editor on `chart` and its Layers section; the editor's pane."""
    chart.hover()
    chart.get_by_role("button", name="Edit chart").click()
    return page.get_by_role("complementary", name="Chart settings")


def test_the_chart_editor_lists_the_layers_a_chart_draws_and_hides_removes_and_adds_them(
    page: Page, live_server_url: str, tmp_path: Path, console_errors: list[str]
) -> None:
    """
    The editor's Layers table has a row for every layer the chart draws, though the chart saved none: set one hidden or
    off, add an artifact as another, and the chart (its legend, and what is drawn) follows. An overlay's row is shown
    by its name, and is the chart's to keep: it cannot be renamed, checked or removed, only restyled.
    """
    _create_project_and_experiment(page, live_server_url, "Layer Editor Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    _log_masks_over_a_picture(live_server_url, tmp_path, experiment_id, extra_layer=True)
    page.reload()
    _auto_generate_charts(page)

    chart = page.locator(".dl-chart-item").filter(has_text="samples/a")  # not the mask's own chart
    thumb = chart.locator(".dl-image-series-thumb")
    legend = thumb.locator(".dl-layer-legend")
    expect(legend.locator(".dl-layer-name")).to_have_text(["prediction", "truth"])

    dock = _open_layers_editor(page, chart)
    rows = dock.locator(".ag-center-cols-container [row-id], .ag-center-cols-container .ag-row").filter(
        has_text="truth"
    )
    expect(dock.locator(".ag-center-cols-container .ag-row")).to_have_count(2)  # saved none, drawn two
    expect(rows).to_have_count(1)
    expect(dock.locator(".ag-row").filter(has_text="$overlay")).to_have_count(0)  # by name, not by key
    for checkbox in dock.locator(".ag-row").locator(".ag-selection-checkbox").all():
        expect(checkbox).to_be_hidden()  # a row the chart keeps has nothing to check, so nothing to remove
    rows.locator('[col-id="key"]').click()  # a click opens the name of a row of the person's own to edit
    expect(page.locator(".dl-key-picker-input")).to_have_count(0)
    dock.get_by_role("button", name="Remove checked").click()
    expect(dock.locator(".ag-center-cols-container .ag-row")).to_have_count(2)

    def set_visibility(layer: str, value: str) -> None:
        row = dock.locator(".ag-center-cols-container .ag-row").filter(has_text=layer)
        radio = row.get_by_role("radio", name=value, exact=True)
        radio.click()  # one click: the choices are in the cell
        expect(radio).to_have_attribute("aria-checked", "true")  # the cell shows what the row now holds

    set_visibility("prediction", "hidden")
    set_visibility("truth", "off")
    add = dock.locator('button[id*="layers"][id*="table-add"]')
    add.click()
    expect(page.locator(".dl-key-picker-input")).to_be_focused()
    page.keyboard.type("mask")  # the artifacts that can be layers are suggested, not the picture itself
    expect(page.locator(".ag-popup").get_by_text("samples/a", exact=True)).to_have_count(0)
    page.locator(".ag-popup").get_by_text("samples/mask").click()
    page.locator(f"#{CHART_EDITOR_SAVE_ID}").click()

    expect(legend.locator(".dl-layer-name")).to_have_text(["prediction", "samples/mask"])  # truth is gone
    expect(_layer_row(legend, "prediction").locator("button")).to_have_attribute("aria-pressed", "false")
    expect(_layer_holder(thumb, "prediction")).to_be_hidden()  # hidden: listed, drawn when a viewer shows it
    expect(_layer_holder(thumb, "truth")).to_have_count(0)  # removed: not there at all
    expect(_layer_holder(thumb, "samples/mask").locator("img")).to_have_count(1)  # the added layer is drawn
    assert console_errors == []  # a layer that could not be drawn is logged, and fails here


def test_a_layer_typed_into_a_new_row_is_kept_when_save_is_pressed(
    page: Page, live_server_url: str, tmp_path: Path, console_errors: list[str]
) -> None:
    """A layer typed into a new row and never confirmed (no Enter, no click away) is still there once Save is pressed."""
    _create_project_and_experiment(page, live_server_url, "Typed Layer Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    _log_masks_over_a_picture(live_server_url, tmp_path, experiment_id, extra_layer=True)
    page.reload()
    _auto_generate_charts(page)

    chart = page.locator(".dl-chart-item").filter(has_text="samples/a")
    dock = _open_layers_editor(page, chart)
    dock.locator('button[id*="layers"][id*="table-add"]').click()
    expect(page.locator(".dl-key-picker-input")).to_be_focused()
    page.keyboard.type("samples/mask")  # typed in full, never committed with Enter or a click away
    page.locator(f"#{CHART_EDITOR_SAVE_ID}").click(delay=100)  # a press that lasts, as a person's does

    legend = chart.locator(".dl-image-series-thumb .dl-layer-legend")
    expect(legend.locator(".dl-layer-name")).to_have_text(["prediction", "truth", "samples/mask"])
    assert console_errors == []


_OVERLAY_COVERS_PICTURE_JS = """(layer) => {
    const picture = layer.parentElement?.querySelector("img")?.getBoundingClientRect();
    const drawn = layer.querySelector("svg, img")?.getBoundingClientRect();
    return Boolean(
        picture && drawn && picture.width > 0 &&
        Math.abs(picture.x - drawn.x) < 1 && Math.abs(picture.y - drawn.y) < 1 &&
        Math.abs(picture.width - drawn.width) < 1 && Math.abs(picture.height - drawn.height) < 1
    );
}"""


def test_an_image_chart_draws_the_overlay_logged_at_each_pictures_step_over_it(
    page: Page, live_server_url: str, tmp_path: Path, console_errors: list[str]
) -> None:
    """
    An auto-generated image chart draws the overlay logged beside its pictures (`Overlay.over`) at each picture's own
    step over it, in the thumbnail and in the enlarged view. An image with several (each named) has them all drawn. A
    step with no overlay shows its picture alone, and each drawing covers its picture.
    """
    _create_project_and_experiment(page, live_server_url, "Overlay Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    picture = np.full((48, 64, 3), 128, dtype=np.uint8)
    # Fractions of a 64 by 48 picture: a box at pixel (8, 12), 24 wide and 12 tall; and a triangle through (40, 12), (56, 12), (48, 42).
    boxes: list[Shape] = [Box(x=0.125, y=0.25, width=0.375, height=0.25, label="cat")]
    triangles: list[Shape] = [
        Polygon(points=[Point(0.625, 0.25), Point(0.875, 0.25), Point(0.75, 0.875)], color="#00ff00"),
    ]
    api.log_artifact_batch(
        [
            Image(key="samples/a", image=picture, step=0, format="jpg").to_artifact(
                tmp_path, run.id, experiment_id
            ),
            Overlay.over("samples/a", name="boxes", shapes=boxes, step=0).to_artifact(
                tmp_path, run.id, experiment_id
            ),
            Overlay.over("samples/a", name="triangles", shapes=triangles, step=0).to_artifact(
                tmp_path, run.id, experiment_id
            ),
            Image(key="samples/a", image=picture, step=10, format="jpg").to_artifact(
                tmp_path, run.id, experiment_id
            ),
        ]
    )
    _wait_for_artifacts(experiment_id, 4)
    page.reload()
    _auto_generate_charts(page)

    # The overlay is drawn by its picture's chart, so auto-generate gives it no chart of its own.
    chart = page.locator(".dl-chart-item")
    expect(chart).to_have_count(1)
    thumb = chart.locator(".dl-image-series-thumb")
    layer = thumb.locator(".dl-image-series-overlay")
    expect(layer.locator("rect")).to_have_count(1)
    expect(layer.locator("polygon")).to_have_count(1)
    expect(layer.locator("svg")).to_have_count(2)  # one per overlay
    expect(layer.locator("svg").first).to_have_attribute(
        "viewBox", "0 0 64 48"
    )  # the picture's own pixel size
    rect = layer.locator("rect")
    assert [rect.get_attribute(a) for a in ("x", "y", "width", "height")] == ["8", "12", "24", "12"]
    page.wait_for_function(_OVERLAY_COVERS_PICTURE_JS, arg=layer.element_handle())

    # Step 10 logged no overlay, so its picture is shown alone; back at step 0 the shapes come back.
    page.locator(".mantine-Slider-thumb").focus()  # not the layers' opacity sliders
    page.keyboard.press("ArrowRight")
    expect(layer.locator("svg")).to_have_count(0)
    page.keyboard.press("ArrowLeft")
    expect(layer.locator("rect")).to_have_count(1)

    thumb.click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    modal_layer = dialog.locator(".dl-image-series-overlay")
    expect(modal_layer.locator("rect")).to_have_count(1)
    page.wait_for_function(_OVERLAY_COVERS_PICTURE_JS, arg=modal_layer.element_handle())
    assert console_errors == []  # a layer or shape that could not be drawn is logged, and fails here


@pytest.mark.parametrize(
    ("colors", "opacity", "left_pixel"),
    [
        pytest.param({1: "#ff0000"}, 0.5, [255, 0, 0, 128], id="colors-at-the-default-opacity"),
        pytest.param(
            None, 1.0, [*bytes.fromhex(DEFAULT_CLASS_COLORS[0][1:]), 255], id="default-palette-opaque"
        ),
    ],
)
def test_an_image_chart_draws_a_mask_in_an_overlay_with_its_classes_in_their_colors(  # noqa: PLR0913 -- a browser test takes its fixtures and its parametrized case
    page: Page,
    live_server_url: str,
    tmp_path: Path,
    console_errors: list[str],
    colors: dict[int, str] | None,
    opacity: float,
    left_pixel: list[int],
) -> None:
    """A run-length encoded mask in an `Overlay` is painted over its picture: its colored class at its opacity, the rest see-through."""
    _create_project_and_experiment(page, live_server_url, f"Mask Overlay Experiment {left_pixel}")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    picture = np.full((48, 64, 3), 128, dtype=np.uint8)
    # The left half of the 48 by 64 picture is class 1, drawn red; the right half is class 0, which has no color.
    labels = np.zeros((1, 48, 64), dtype=np.int64)  # one class-dimension of the picture's height and width
    labels[:, :, :32] = 1
    mask = rle.run_length_encode(labels, colors=colors, classnames={1: "cat"}, opacity=opacity)
    api.log_artifact_batch(
        [
            Image(key="samples/a", image=picture, step=0, format="jpg").to_artifact(
                tmp_path, run.id, experiment_id
            ),
            Overlay.over("samples/a", name="mask", shapes=[mask], step=0).to_artifact(
                tmp_path, run.id, experiment_id
            ),
        ]
    )
    _wait_for_artifacts(experiment_id, 2)
    page.reload()
    _auto_generate_charts(page)

    layer = page.locator(".dl-chart-item .dl-image-series-thumb .dl-image-series-overlay")
    expect(layer.locator("image")).to_have_count(1)
    href = layer.locator("image").get_attribute("href")
    # Sample the drawn mask's pixels: the left (class 1) is in its color at the mask's opacity, the right (class 0) is see-through.
    pixels = page.evaluate(
        """async (href) => {
            const img = new Image();
            img.src = href;
            await img.decode();
            const canvas = document.createElement("canvas");
            canvas.width = img.width;
            canvas.height = img.height;
            const context = canvas.getContext("2d");
            context.drawImage(img, 0, 0);
            const at = (x, y) => Array.from(context.getImageData(x, y, 1, 1).data);
            return [at(4, 4), at(60, 44)];
        }""",
        href,
    )
    assert pixels == [left_pixel, [0, 0, 0, 0]]
    assert console_errors == []  # a mask that could not be drawn is logged, and fails here


def test_an_image_chart_draws_an_image_logged_beside_its_pictures_over_them_as_a_layer(
    page: Page, live_server_url: str, tmp_path: Path
) -> None:
    """Any artifact can be a layer: an image logged beside a picture (a mask, say) is drawn over it, scaled with it."""
    _create_project_and_experiment(page, live_server_url, "Mask Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    mask = np.zeros((12, 16, 3), dtype=np.uint8)
    mask[3:9, 4:12] = (255, 0, 0)
    api.log_artifact_batch(
        [
            Image(
                key="samples/a", image=np.full((48, 64, 3), 128, dtype=np.uint8), step=0, format="jpg"
            ).to_artifact(tmp_path, run.id, experiment_id),
            Image(key=f"samples/a{OVERLAY_KEY_SUFFIX}", image=mask, step=0).to_artifact(
                tmp_path, run.id, experiment_id
            ),
        ]
    )
    _wait_for_artifacts(experiment_id, 2)
    page.reload()
    _auto_generate_charts(page)

    chart = page.locator(".dl-chart-item")
    expect(chart).to_have_count(1)  # the mask is a layer of its picture's chart, not a chart of its own
    layer = chart.locator(".dl-image-series-thumb .dl-image-series-overlay")
    expect(layer.locator(".dl-image-series-layer img")).to_have_count(1)
    page.wait_for_function(_OVERLAY_COVERS_PICTURE_JS, arg=layer.element_handle())


def test_an_image_charts_runs_follow_their_panels_grid_columns(
    page: Page, live_server_url: str, tmp_path: Path
) -> None:
    """The columns an image chart puts its runs in are the panel's, at once when changed and when rebuilt."""
    _create_project_and_experiment(page, live_server_url, "Image Columns Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    for _ in range(4):
        run = api.create_run(models.NewRun(experiment_id=experiment_id))
        image = Image(key="samples/a", image=np.zeros((8, 8, 3), dtype=np.uint8), step=0)
        api.log_artifact_batch([image.to_artifact(tmp_path, run.id, experiment_id)])
    _wait_for_artifacts(experiment_id, 4)
    page.reload()
    _auto_generate_charts(page)
    runs = page.locator("[data-dl-follow-panel-columns]").first
    expect(runs).to_have_css("grid-template-columns", re.compile(r"^\S+ \S+ \S+$"))

    _open_panel_menu(page)
    page.get_by_text("Grid", exact=True).first.click()
    expect(page.locator(".dl-panel-body .mantine-SimpleGrid-root").first).to_be_visible()
    _open_panel_menu(page)
    columns = page.get_by_role("textbox", name="Grid columns")
    columns.fill("2")
    columns.press("Enter")
    expect(runs).to_have_css("grid-template-columns", re.compile(r"^\S+ \S+$"))

    page.reload()
    expect(runs).to_have_css("grid-template-columns", re.compile(r"^\S+ \S+$"))


def test_a_tables_numbers_are_shown_by_what_they_are(page: Page, live_server_url: str) -> None:
    """
    Whole numbers as they are, small ones to four significant digits.

    dash-ag-grid silently ignores a formatter its expression parser can't evaluate (it shows the raw
    value), so only a real grid says the formatter works.
    """
    _create_project_and_experiment(page, live_server_url, "Table Numbers Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    shown = {  # metric: (logged, as shown)
        "loss": (0.3509123, "0.3509"),
        "big": (12.34567, "12.346"),
        "whole": (7.0, "7"),
        "tiny": (0.0001, "0.0001"),
        "tinier": (1.5e-7, "1.5e-7"),
        "negative": (-0.25, "-0.25"),
    }
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=0,
                metrics={name: logged for name, (logged, _) in shown.items()},
                timestamp_utc=pendulum.now("UTC"),
            )
        ]
    )
    store = get_system_data_store()
    shared = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    table = models.ChartInstance[Any, Any](chart_type="table", parameters={})
    store.update_page(
        shared.model_copy(update={"panels": [PanelInstance[Any, Any](name="Numbers", charts=[table])]})
    )
    page.reload()

    for name, (_, expected) in shown.items():
        expect(page.locator(f".dl-chart-item .ag-cell[col-id='{name}']")).to_have_text(expected)


def test_a_tables_sort_filter_and_page_survive_a_live_update_redrawing_it(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    A live update replaces a changed chart's whole body, which mounts a fresh AG Grid; the table's grid
    is persisted (and put back on its page) so that what the viewer did to it is still there. Only a
    browser shows whether Dash's persistence really restores an AG Grid's sort and filter on a remount.
    """
    _create_project_and_experiment(page, live_server_url, "Table State Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])

    api = BasicDlboardAPI(live_server_url)
    runs = [
        api.create_run(models.NewRun(experiment_id=experiment_id, name=f"{kind}-{n}"))
        for n, kind in enumerate(["keep"] * 5 + ["drop"] * 3)
    ]

    def log_loss(run: models.Run, step: int, loss: float) -> None:
        api.log_metric_batch(
            [
                models.LoggedMetrics(
                    experiment_id=experiment_id,
                    run_id=run.id,
                    step=step,
                    metrics={"loss": loss},
                    timestamp_utc=pendulum.now("UTC"),
                )
            ]
        )

    for n, run in enumerate(runs):
        log_loss(run, 0, float(n))
    store = get_system_data_store()
    shared = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    table = models.ChartInstance[Any, Any](chart_type="table", parameters={"page_size": 3})
    store.update_page(
        shared.model_copy(update={"panels": [PanelInstance[Any, Any](name="Runs", charts=[table])]})
    )
    page.reload()

    chart = page.locator(".dl-chart-item")
    grid = chart.locator(".ag-root-wrapper")
    expect(grid).to_be_visible()
    # Filter to the five "keep" runs (two pages of three), sort by loss descending, and go to page 2.
    chart.locator(".ag-floating-filter[col-id='run_name'] input").fill("keep")
    loss_header = chart.get_by_role("columnheader", name="loss")
    loss_header.locator(".ag-header-cell-label").click()
    loss_header.locator(".ag-header-cell-label").click()
    expect(loss_header).to_have_attribute("aria-sort", "descending")
    chart.get_by_role("button", name="Next Page").click()
    expect(chart.locator(".ag-paging-description")).to_contain_text("2")
    expect(chart.locator(".ag-center-cols-container .ag-row")).to_have_count(2)

    grid.evaluate("el => el.setAttribute('data-marker', 'before-the-poll')")
    log_loss(runs[0], 1, 99.0)  # the poll redraws the table: its data moved

    expect(grid).not_to_have_attribute("data-marker", "before-the-poll", timeout=8000)
    expect(loss_header).to_have_attribute("aria-sort", "descending")
    expect(chart.locator(".ag-floating-filter[col-id='run_name'] input")).to_have_value("keep")
    expect(chart.locator(".ag-center-cols-container .ag-row")).to_have_count(2)
    expect(chart.locator(".ag-paging-description")).to_contain_text("2")
    assert console_errors == []


def test_a_table_of_artifacts_shows_thumbnails_and_sorts_by_a_tag(
    page: Page, live_server_url: str, tmp_path: Path, console_errors: list[str]
) -> None:
    """The thumbnails really load and are fitted to their rows, and a numeric tag column sorts as a number."""
    _create_project_and_experiment(page, live_server_url, "Artifact Table Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    tall = np.zeros((400, 100, 3), dtype=np.uint8)
    for score in (2.0, 10.0, 9.0):
        run = api.create_run(models.NewRun(experiment_id=experiment_id))
        image = Image(key="eval/sample", image=tall, step=0, tags={"score": score})
        api.log_artifact_batch([image.to_artifact(tmp_path, run.id, experiment_id)])
    _wait_for_artifacts(experiment_id, 3)

    store = get_system_data_store()
    shared = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    table = models.ChartInstance[Any, Any](
        chart_type="table",
        parameters={"mode": "artifacts", "artifact_key": "eval/", "artifact_key_is_prefix": True},
    )
    store.update_page(
        shared.model_copy(update={"panels": [PanelInstance[Any, Any](name="Samples", charts=[table])]})
    )
    page.reload()

    chart = page.locator(".dl-chart-item")
    images = chart.locator(".ag-cell img")
    expect(images).to_have_count(3)
    for index in range(3):
        box = images.nth(index).bounding_box()
        assert box is not None
        assert 0 < box["height"] <= 56  # fitted to the row, not the 400px it was logged at
        assert images.nth(index).evaluate("img => img.naturalWidth") > 0

    score = chart.get_by_role("columnheader", name="score")
    score.locator(".ag-header-cell-label").click()
    score.locator(".ag-header-cell-label").click()
    expect(score).to_have_attribute("aria-sort", "descending")
    # The DOM keeps rows in data order; `row-index` is where each is shown.
    for row_index, expected in enumerate(["10", "9", "2"]):  # numbers, not text: 9 sorts after 10, before 2
        shown = chart.locator(f".ag-center-cols-container .ag-row[row-index='{row_index}'] .ag-cell")
        expect(shown.and_(page.locator("[col-id='artifact__tag__score']"))).to_have_text(expected)
    assert console_errors == []


def test_a_tables_editor_opens_an_old_table_as_its_new_settings_and_takes_a_typed_prefix(
    page: Page, live_server_url: str, tmp_path: Path
) -> None:
    """A table saved with the old `mode` settings is edited as the ones that replaced them, and a prefix can be typed."""
    _create_project_and_experiment(page, live_server_url, "Table Editor Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    pixels = np.zeros((8, 8, 3), dtype=np.uint8)
    api.log_artifact_batch(
        [
            Image(key=key, image=pixels, step=0).to_artifact(tmp_path, run.id, experiment_id)
            for key in ("eval/a", "eval/b", "other/c")
        ]
    )
    _wait_for_artifacts(experiment_id, 3)

    store = get_system_data_store()
    shared = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    table = models.ChartInstance[Any, Any](
        chart_type="table",
        parameters={"mode": "artifacts", "artifact_key": "eval/", "artifact_key_is_prefix": True},
    )
    store.update_page(
        shared.model_copy(update={"panels": [PanelInstance[Any, Any](name="Samples", charts=[table])]})
    )
    page.reload()

    chart = page.locator(".dl-chart-item")
    images = chart.locator(".ag-cell img")
    expect(images).to_have_count(2)
    chart.hover()
    chart.get_by_role("button", name="Edit chart").click()

    editor = page.get_by_role("complementary", name="Chart settings")
    shown = editor.locator(".mantine-Pill-root", has_text="eval/*")
    expect(shown).to_have_count(1)  # the old `artifact_key` and `artifact_key_is_prefix`, as one prefix
    shown.locator(".mantine-Pill-remove").click()
    editor.get_by_label("Show artifacts").fill("other/*")  # not a key: a prefix, typed
    page.keyboard.press("Enter")
    page.locator(f"#{CHART_EDITOR_SAVE_ID}").click()

    expect(images).to_have_count(1)


def test_logged_histograms_render_in_each_display_and_the_step_slider_moves(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Histograms binned on the client, shipped over REST and drawn by Plotly in a browser.

    What a Python test can't see: that Plotly accepts the figures each display builds, draws them, and that the
    step view's slider (frames, not a callback) moves the figure on without a server round trip.
    """
    _create_project_and_experiment(page, live_server_url, "Histogram Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    rng = np.random.default_rng(0)
    binning = ExponentialBinning()
    steps = 12
    for _ in range(2):
        run = api.create_run(models.NewRun(experiment_id=experiment_id))
        api.log_metric_batch(
            [
                models.LoggedMetrics(
                    experiment_id=experiment_id,
                    run_id=run.id,
                    step=step,
                    # Shrinking over the steps, as gradients do.
                    histograms={
                        "grad": binning.histogram(
                            cast("list[float]", (rng.standard_normal(2000) * 10.0 ** (-step / 6)).tolist())
                        )
                    },
                    timestamp_utc=pendulum.now("UTC"),
                )
                for step in range(steps)
            ]
        )
    store = get_system_data_store()
    shared = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    charts = [
        models.ChartInstance[Any, Any](chart_type="histogram", parameters={"key": "grad", "display": display})
        for display in ("heatmap", "bands", "step")
    ]
    store.update_page(
        shared.model_copy(update={"panels": [PanelInstance[Any, Any](name="Gradients", charts=charts)]})
    )
    page.reload()

    heatmap, bands, step = (page.locator(".dl-histogram").nth(i) for i in range(3))
    expect(heatmap.locator(".heatmaplayer image")).to_have_count(2)  # a row per run
    expect(bands.locator(".scatterlayer .trace")).to_have_count(2 * 5)
    expect(step.locator(".barlayer .trace")).to_have_count(2)
    expect(step.locator(".annotation-text", has_text=f"step {steps - 1}")).to_be_visible()
    step.locator(".slider-rail-touch-rect").click(position={"x": 9, "y": 16})
    expect(step.locator(".annotation-text", has_text="step 0")).to_be_visible()
    assert console_errors == []


def _log_two_metrics(live_server_url: str, experiment_id: int) -> None:
    run = BasicDlboardAPI(live_server_url).create_run(models.NewRun(experiment_id=experiment_id))
    BasicDlboardAPI(live_server_url).log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=step,
                metrics={"train/loss": 1.0 / (step + 1), "train/acc": step / 3},
                timestamp_utc=pendulum.now("UTC"),
            )
            for step in range(3)
        ]
    )


def test_a_chart_being_edited_is_drawn_in_its_own_card_beside_the_page_and_cancelling_restores_it(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    The editor is docked beside the panels, so the page stays in view. The chart being edited is drawn as the form has
    it in its own card, in place of the saved chart, which stays under it; cancelling puts the saved chart back as it was.
    """
    _create_project_and_experiment(page, live_server_url, "Docked Editor Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    _log_two_metrics(live_server_url, experiment_id)
    page.reload()
    _auto_generate_charts(page)

    charts = page.locator(".dl-chart-item")
    expect(charts).to_have_count(2)
    edited, other = charts.nth(0), charts.nth(1)
    saved = edited.locator(".dl-chart-body .mantine-LineChart-root")
    drawn = edited.locator(".dl-chart-draft .mantine-LineChart-root")
    expect(saved).to_have_css("height", "300px")
    dock = page.get_by_role("complementary", name="Chart settings")
    expect(dock).to_be_hidden()

    edited.hover()
    edited.get_by_role("button", name="Edit chart").click()
    expect(edited).to_have_attribute("data-dl-editing", "")  # outlined as it opens
    dock.get_by_role("button", name="Display").click()
    dock.get_by_label("Height").fill("500")

    expect(drawn).to_have_css("height", "500px")  # the card shows the draft...
    expect(saved).to_be_hidden()  # ...and not the saved chart, which is still there under it
    expect(other).to_be_visible()  # the rest of the page stays in view

    dock.get_by_role("button", name="Cancel").click()

    expect(dock).to_be_hidden()
    expect(edited.locator(".dl-chart-draft")).to_be_empty()
    expect(saved).to_have_css("height", "300px")
    assert console_errors == []


def test_switching_the_editor_to_a_chart_in_another_panel_moves_the_outline_and_drops_the_old_draft(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """Opening an edit on another chart is quick and clean: no chart is drawn from the previous form's values."""
    _create_project_and_experiment(page, live_server_url, "Dock Switch Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    run = BasicDlboardAPI(live_server_url).create_run(models.NewRun(experiment_id=experiment_id))
    BasicDlboardAPI(live_server_url).log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=step,
                metrics={"a/loss": 1.0 / (step + 1), "b/loss": 2.0 / (step + 1)},
                timestamp_utc=pendulum.now("UTC"),
            )
            for step in range(3)
        ]
    )
    page.reload()
    _auto_generate_charts(page)
    page.get_by_role("button", name="b", exact=True).click()  # open the second panel
    charts = page.locator(".dl-chart-item")
    expect(charts).to_have_count(2)
    first, second = charts.nth(0), charts.nth(1)
    dock = page.get_by_role("complementary", name="Chart settings")

    first.hover()
    first.get_by_role("button", name="Edit chart").click()
    dock.get_by_role("button", name="Display").click()
    dock.get_by_label("Height").fill("500")
    expect(first.locator(".dl-chart-draft .mantine-LineChart-root")).to_have_css("height", "500px")

    second.hover()
    second.get_by_role("button", name="Edit chart").click()

    expect(second).to_have_attribute("data-dl-editing", "")
    expect(first).not_to_have_attribute("data-dl-editing", "")
    expect(first.locator(".dl-chart-draft")).to_be_empty()  # the first chart is as it was saved
    expect(first.locator(".dl-chart-body .mantine-LineChart-root")).to_have_css("height", "300px")
    expect(dock.get_by_label("Column *")).to_have_value("b/loss")
    # The second is drawn from its own form, not the first's (which had a height of 500).
    expect(second.locator(".dl-chart-draft .mantine-LineChart-root")).to_have_css("height", "300px")
    assert console_errors == []


def test_a_chart_is_added_from_the_dock_with_its_preview_there_and_lands_at_the_end_of_its_panel(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """A chart being added has no card yet, so the dock previews it; saving adds it, and closes the dock."""
    _create_project_and_experiment(page, live_server_url, "Dock Add Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    _log_two_metrics(live_server_url, experiment_id)
    page.reload()
    _auto_generate_charts(page)
    charts = page.locator(".dl-chart-item")
    expect(charts).to_have_count(2)

    page.get_by_role("button", name="Add chart to this panel").first.click()
    dock = page.get_by_role("complementary", name="Chart settings")
    expect(dock.locator("#chart-editor-title")).to_have_text("Add chart")
    dock.get_by_label("Chart type").click()
    page.get_by_role("option", name="line", exact=True).click()
    dock.get_by_label("Column *").click()
    page.get_by_role("option", name="train/acc").click()
    dock.get_by_label("X axis *").click()
    page.get_by_role("option", name="step", exact=True).click()

    expect(dock.locator(".mantine-LineChart-root")).to_be_visible()  # the preview is in the dock
    dock.get_by_role("button", name="Add chart").click()

    expect(dock).to_be_hidden()
    expect(charts).to_have_count(3)
    assert console_errors == []


def test_a_live_update_while_a_chart_is_being_edited_redraws_the_chart_under_its_draft_not_the_draft(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """The poll replaces a chart's body, never its draft, so what is being edited is not clobbered by new data."""
    _create_project_and_experiment(page, live_server_url, "Draft Poll Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=0,
                metrics={"hot_metric": 1.0},
                timestamp_utc=pendulum.now("UTC"),
            )
        ]
    )
    page.reload()
    _auto_generate_charts(page)

    item = page.locator(".dl-chart-item")
    item.hover()
    item.get_by_role("button", name="Edit chart").click()
    draft = item.locator(":scope > .dl-chart-draft > *").first
    expect(draft).to_be_attached()
    body_chart = item.locator(":scope > .dl-chart-body > *").first
    for node in (draft, body_chart):
        node.evaluate("el => el.setAttribute('data-marker', 'untouched')")

    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment_id,
                run_id=run.id,
                step=1,
                metrics={"hot_metric": 2.0},
                timestamp_utc=pendulum.now("UTC"),
            )
        ]
    )

    expect(body_chart).not_to_have_attribute(
        "data-marker", "untouched", timeout=8000
    )  # the chart underneath is redrawn
    assert draft.get_attribute("data-marker") == "untouched"  # the draft is not
    assert console_errors == []


def _height_of(locator: Locator) -> float:
    """How tall the element is on screen."""
    box = locator.bounding_box()
    assert box is not None
    return box["height"]


def _layer_key(name: str) -> str:
    """The artifact key of the layer shown as `name`: an overlay of `samples/a` by its name, or an artifact by its key."""
    return name if name == "samples/mask" else overlay_key("samples/a", name)


def _layer_row(legend: Locator, name: str) -> Locator:
    """The row of layer `name` in a picture's legend."""
    return legend.locator(f'.dl-layer-row[data-dl-layer="{_layer_key(name)}"]')


def _layer_holder(thumb: Locator, name: str) -> Locator:
    """The element layer `name` is drawn in over a picture."""
    return thumb.locator(f'.dl-image-series-layer[data-dl-layer="{_layer_key(name)}"]')


def test_a_layer_is_hidden_and_shown_from_the_legend_without_fetching_and_the_change_can_be_saved_or_reset(  # noqa: PLR0915 -- one flow
    page: Page, live_server_url: str, tmp_path: Path, console_errors: list[str]
) -> None:
    """
    Each layer has a chip in the chart's legend: clicking it shows or hides the layer at once, drawn already so with no
    request, and the change is the viewer's own (kept for the tab, it survives a reload). The card offers to save it as
    the chart's own settings, or to reset it.
    """
    _create_project_and_experiment(page, live_server_url, "Legend Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    picture = np.full((48, 64, 3), 128, dtype=np.uint8)
    boxes: list[Shape] = [Box(x=0.125, y=0.25, width=0.375, height=0.25, label="cat")]
    triangles: list[Shape] = [
        Polygon(points=[Point(0.625, 0.25), Point(0.875, 0.25), Point(0.75, 0.875)], color="#00ff00")
    ]
    api.log_artifact_batch(
        [
            Image(key="samples/a", image=picture, step=0, format="jpg").to_artifact(
                tmp_path, run.id, experiment_id
            ),
            Overlay.over("samples/a", name="boxes", shapes=boxes, step=0).to_artifact(
                tmp_path, run.id, experiment_id
            ),
            Overlay.over("samples/a", name="triangles", shapes=triangles, step=0).to_artifact(
                tmp_path, run.id, experiment_id
            ),
        ]
    )
    _wait_for_artifacts(experiment_id, 3)
    page.reload()
    _auto_generate_charts(page)

    chart = page.locator(".dl-chart-item")
    layer = chart.locator(".dl-image-series-thumb .dl-image-series-overlay")
    boxes_chip = chart.get_by_role("button", name="boxes", exact=True)
    height = _height_of(chart)
    legend = chart.locator(".dl-layer-legend").first
    expect(
        legend.locator(".dl-layer-rows")
    ).to_be_hidden()  # starts folded to its title: the picture is clear
    legend.get_by_role("button", name="Layers").click()
    expect(chart.locator(".dl-layer-toggle")).to_have_count(2)  # a row for each layer, over the picture
    expect(boxes_chip).to_have_attribute("aria-pressed", "true")
    expect(layer.locator("rect")).to_be_visible()
    bar = chart.locator(".dl-chart-view-bar")
    expect(bar).to_be_hidden()

    requested: list[str] = []
    page.on("request", lambda request: requested.append(request.url))
    boxes_chip.click()
    expect(layer.locator("rect")).to_be_hidden()
    expect(layer.locator("polygon")).to_be_visible()  # the other layer is as it was
    boxes_chip.click()
    expect(layer.locator("rect")).to_be_visible()
    boxes_chip.click()
    expect(boxes_chip).to_have_attribute("aria-pressed", "false")
    assert not [
        url for url in requested if "/artifact/" in url
    ]  # drawn already: showing it again fetches nothing

    expect(bar).to_be_visible()
    assert (chart.bounding_box() or {})[
        "height"
    ] == height  # nothing moved: the legend and the bar are over the chart
    page.reload()  # the viewer's own change is kept for the tab
    expect(boxes_chip).to_have_attribute("aria-pressed", "false")
    expect(layer.locator("rect")).to_be_hidden()

    bar.get_by_role("button", name="Save as default").click()  # now it is the chart's own settings
    expect(bar).to_be_hidden()
    page.evaluate("window.sessionStorage.clear()")
    page.reload()
    legend.get_by_role(
        "button", name="Layers"
    ).click()  # the session was cleared, so the legend is folded again
    expect(boxes_chip).to_have_attribute("aria-pressed", "false")
    expect(layer.locator("rect")).to_be_hidden()

    boxes_chip.click()  # showing it is a change from what is saved: reset goes back to the saved chart
    expect(layer.locator("rect")).to_be_visible()
    expect(bar).to_be_visible()
    assert (chart.bounding_box() or {})[
        "height"
    ] == height  # nothing moved: the legend and the bar are over the chart
    bar.get_by_role("button", name="Reset").click()
    expect(boxes_chip).to_have_attribute("aria-pressed", "false")
    expect(layer.locator("rect")).to_be_hidden()
    expect(bar).to_be_hidden()
    assert console_errors == []


def test_a_layer_shown_or_hidden_in_one_view_of_an_experiment_is_not_so_in_another(
    page: Page, live_server_url: str, tmp_path: Path, console_errors: list[str]
) -> None:
    """The same chart in two views of one experiment keeps the viewer's changes to its layers apart."""
    _create_project_and_experiment(page, live_server_url, "Two Views Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    _log_masks_over_a_picture(live_server_url, tmp_path, experiment_id)
    page.reload()
    _auto_generate_charts(page)

    chart = page.locator(".dl-chart-item").filter(has_text="samples/a")
    legend = chart.locator(".dl-layer-legend").first
    legend.get_by_role("button", name="Layers").click()
    truth = _layer_row(legend, "truth").locator("button")
    bar = chart.locator(".dl-chart-view-bar")
    truth.click()
    expect(truth).to_have_attribute("aria-pressed", "false")
    expect(bar).to_be_visible()

    # A second view, of the very charts this one has (same ids), as someone else's link would open it.
    here = parse_qs(urlparse(page.url).query).get("view")
    store = get_system_data_store()
    current = (
        store.get_view(BasicExperimentPage, int(here[0]))
        if here
        else store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    )
    assert current is not None
    other = store.create_view(
        BasicExperimentPage,
        models.NewPage[Any, Any](
            experiment_id=experiment_id,
            owner_id=store.get_or_create_user(models.Principal.unverified("second-view")).id,
            name="second view",
            panels=current.panels,
        ),
    )
    first_url = page.url

    page.goto(f"{live_server_url}/experiment/{experiment_id}?view={other.id}")
    legend.get_by_role("button", name="Layers").click()
    expect(truth).to_have_attribute("aria-pressed", "true")  # the first view's change is not this view's
    expect(bar).to_be_hidden()
    _layer_row(legend, "prediction").locator("button").click()
    expect(bar).to_be_visible()

    page.goto(first_url)  # each view still has its own
    legend.get_by_role("button", name="Layers").click()
    expect(truth).to_have_attribute("aria-pressed", "false")
    expect(_layer_row(legend, "prediction").locator("button")).to_have_attribute("aria-pressed", "true")
    assert console_errors == []


def test_the_legend_over_a_picture_lists_what_each_layer_holds_and_hides_peeks_and_solos_them(  # noqa: PLR0915 -- one flow
    page: Page, live_server_url: str, tmp_path: Path, console_errors: list[str]
) -> None:
    """
    The legend is over the picture it is about, and lists what each layer holds -- a class of one layer is not a class of
    another, however alike. An eye hides every layer and holding a picture peeks without them; shift-clicking a layer shows
    it alone; a layer the picture on show lacks is dimmed; and the legend folds down to its title.
    """
    _create_project_and_experiment(page, live_server_url, "Legend Details Experiment")
    page.locator(".experiment-card").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    picture = np.full((48, 64, 3), 128, dtype=np.uint8)
    labels = np.zeros((1, 48, 64), dtype=np.int64)
    labels[:, :, :32] = 1
    # The same class in the same color in two layers, as a truth and a prediction have.
    truth = rle.run_length_encode(labels, colors={1: "#ff0000"}, classnames={1: "cat"})
    prediction = rle.run_length_encode(labels, colors={1: "#ff0000"}, classnames={1: "cat"})
    boxes: list[Shape] = [Box(x=0.125, y=0.25, width=0.375, height=0.25, label="dog")]
    api.log_artifact_batch(
        [
            Image(key="samples/a", image=picture, step=0, format="jpg").to_artifact(
                tmp_path, run.id, experiment_id
            ),
            Overlay.over("samples/a", name="truth", shapes=[truth], step=0).to_artifact(
                tmp_path, run.id, experiment_id
            ),
            Overlay.over("samples/a", name="prediction", shapes=[prediction], step=0).to_artifact(
                tmp_path, run.id, experiment_id
            ),
            Overlay.over("samples/a", name="boxes", shapes=boxes, step=0).to_artifact(
                tmp_path, run.id, experiment_id
            ),
            Image(key="samples/a", image=picture, step=10, format="jpg").to_artifact(
                tmp_path, run.id, experiment_id
            ),
        ]
    )
    _wait_for_artifacts(experiment_id, 5)
    page.reload()
    _auto_generate_charts(page)

    chart = page.locator(".dl-chart-item")
    thumb = chart.locator(".dl-image-series-thumb")
    legend = thumb.locator(".dl-layer-legend")
    expect(
        legend.locator(".dl-layer-rows")
    ).to_be_hidden()  # starts folded to its title: the picture is clear
    legend.get_by_role("button", name="Layers").click()
    expect(_layer_row(legend, "truth").locator(".dl-class")).to_have_text(["cat"])
    expect(_layer_row(legend, "prediction").locator(".dl-class")).to_have_text(
        ["cat"]
    )  # its own cat, not truth's
    expect(_layer_row(legend, "boxes").locator(".dl-class")).to_have_text(["dog"])

    # Over the picture, not beside it or above the chart: inside the picture's own corner.
    shown, over = thumb.locator("img").bounding_box(), legend.bounding_box()
    assert shown is not None
    assert over is not None
    assert shown["x"] <= over["x"]
    assert shown["y"] <= over["y"]
    assert over["x"] + over["width"] <= shown["x"] + shown["width"]
    assert over["y"] + over["height"] <= shown["y"] + shown["height"]

    eye = legend.get_by_role("button", name="Overlays")
    eye.click()
    expect(_layer_holder(thumb, "truth")).to_be_hidden()
    expect(legend.locator(".dl-layer-classes .dl-class")).to_have_count(0)  # nothing shown, nothing listed
    expect(eye).to_have_attribute("aria-pressed", "false")
    eye.click()
    expect(legend.locator(".dl-layer-classes .dl-class")).to_have_count(3)

    _layer_row(legend, "prediction").get_by_role("button").click(
        modifiers=["Shift"]
    )  # compare one with the rest: show it alone
    expect(_layer_holder(thumb, "prediction")).to_be_visible()
    expect(_layer_holder(thumb, "truth")).to_be_hidden()
    expect(_layer_holder(thumb, "boxes")).to_be_hidden()
    _layer_row(legend, "prediction").get_by_role("button").click(
        modifiers=["Shift"]
    )  # and again: every layer is back
    expect(_layer_holder(thumb, "truth")).to_be_visible()
    expect(_layer_holder(thumb, "boxes")).to_be_visible()

    box = thumb.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] - 4, box["y"] + box["height"] - 4)
    page.mouse.down()  # held: the picture shows without its layers
    expect(chart.locator(".dl-image-series")).to_have_attribute("data-dl-peek", "")
    expect(thumb.locator(".dl-image-series-overlay")).to_have_css("visibility", "hidden")
    page.mouse.up()
    expect(chart.locator(".dl-image-series")).not_to_have_attribute("data-dl-peek", "")
    expect(page.get_by_role("dialog")).to_be_hidden()  # a hold is not a click: the picture was not opened

    legend.get_by_role("button", name="Layers").click()  # fold it down to its title
    expect(_layer_row(legend, "truth")).to_be_hidden()
    legend.get_by_role("button", name="Layers").click()
    expect(_layer_row(legend, "truth")).to_be_visible()

    page.locator(".mantine-Slider-thumb").focus()  # not the picture: step 10 logged no layers
    page.keyboard.press("ArrowRight")
    expect(_layer_row(legend, "truth")).to_have_attribute("data-dl-absent", "")
    assert console_errors == []
