"""
Browser-driven end-to-end regression tests for the composed dltrack app.

Exercises the app the way a real deployment is wired -- `dltrack.serve.app.app()` with the same
plugin bundle `dltrack serve local` uses -- through an actual rendered browser (Playwright). A real browser
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

import time
from typing import TYPE_CHECKING

import pendulum
import pytest
from playwright.sync_api import expect

from dltrack import models
from dltrack.plugins.backend.basic_rest_backend import BasicDltrackAPI
from dltrack.serve import get_data_store
from dltrack.serve._pages._experiment._experiment_page_state import (
    LIVE_PAUSED_BADGE_ID,
    LIVE_STATUS_ID,
    LIVE_UPDATES_ENABLED_ID,
    METRIC_CONTENT_ID,
    NEW_PANEL_ID,
    NEW_PANEL_NAME_ID,
    NEW_TAB_BUTTON_ID,
    NEW_TAB_PANELS_SELECT_ID,
    PAGE_EXPERIMENT_ID,
    RENAME_TAB_BUTTON_ID,
    RENAME_TAB_NAME_INPUT_ID,
    BasicExperimentPage,
)
from dltrack.serve._pages._experiment._run_comparison_table import (
    NAVBAR_HPARAM_COL_SELECT_ID,
    NAVBAR_HPARAM_COLUMNS_TOGGLE_ID,
    NAVBAR_HPARAM_CONFIRM_COLS_ID,
    NAVBAR_HPARAM_DATATABLE_ID,
)
from dltrack.serve._pages._simple_homepage import NEW_PROJECT_BUTTON_ID, NEW_PROJECT_NAME_ID
from dltrack.serve._pages._simple_project_page import NEW_EXP_BUTTON_ID, NEW_EXP_NAME_ID
from dltrack.serve.app import PAGE_LOADING_CLASS

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from playwright.sync_api import Page

pytestmark = pytest.mark.browser


def _create_project_and_experiment(page: Page, live_server_url: str, name: str) -> None:
    """
    Drive the real homepage/project-page forms to create `name` for both project and experiment.

    Both tests in this module share one session-scoped app/db (see `dltrack_app`), so the homepage
    and project page each accumulate a card per test that's run before this one -- every lookup
    here is scoped to the card just created by `name`, not "the" project/experiment card.
    """
    page.goto(live_server_url)
    page.locator(f"#{NEW_PROJECT_NAME_ID}").fill(name)
    page.locator(f"#{NEW_PROJECT_BUTTON_ID}").click()
    page.locator(".project-card", has_text=name).get_by_role("link", name="Open project").click()

    page.locator(f"#{NEW_EXP_NAME_ID}").fill(name)
    page.locator(f"#{NEW_EXP_BUTTON_ID}").click()


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
    page.get_by_role("link", name="Open experiment").click()

    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDltrackAPI(live_server_url)
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
    page.get_by_role("button", name="Auto-generate charts").click()
    # No explicit `open_panel` setting exists yet for a brand new experiment, so
    # `BasicExperimentPage.render()`'s own fallback opens the first (and here, only) panel by
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
    store = get_data_store()
    fetch_hyperparams = store.fetch_hyperparams

    def slow_fetch_hyperparams(experiment_id: int) -> Iterator[models.HyperParams]:
        time.sleep(2)
        return fetch_hyperparams(experiment_id)

    monkeypatch.setattr(store, "fetch_hyperparams", slow_fetch_hyperparams)
    page.get_by_role("link", name="Open experiment").click()

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
    page.get_by_role("link", name="Open experiment").click()

    for panel_name in ("keep", "delete-me"):
        page.locator(f"#{NEW_PANEL_NAME_ID}").fill(panel_name)
        page.locator(f"#{NEW_PANEL_ID}").click()
        # Each click's callback bases its mutation on the client-side page-state snapshot, so the
        # next click must wait for this one's response to land first -- otherwise the second
        # request reads a stale (pre-mutation) snapshot and its response clobbers the first.
        expect(page.get_by_role("button", name=panel_name)).to_be_visible()

    # "keep" (created first) starts open -- no explicit `open_panel` setting exists yet for a
    # brand new experiment, so `BasicExperimentPage.render()`'s own fallback opens the first panel
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
    delete_me_trash_icon = page.get_by_role("button", name="🗑").nth(1)
    delete_me_trash_icon.hover()
    delete_me_trash_icon.click()

    confirm_text = page.get_by_text("Delete this panel?")
    expect(confirm_text).to_be_visible()
    page.get_by_role("button", name="Cancel").click()
    expect(confirm_text).not_to_be_visible()
    expect(page.get_by_text("delete-me")).to_be_visible()  # Cancel left it alone

    delete_me_trash_icon.click()
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
    page.get_by_role("link", name="Open experiment").click()
    page.get_by_role("button", name="Delete experiment").click()
    page.get_by_role("button", name="Delete", exact=True).click()

    expect(page).to_have_url(project_url)
    expect(page.get_by_role("link", name="Open experiment")).to_have_count(0)

    page.goto(f"{live_server_url}/admin")
    page.get_by_role("button", name="Restore").click()
    expect(page.get_by_role("button", name="Restore")).to_have_count(0)

    page.goto(project_url)
    expect(page.get_by_role("link", name="Open experiment")).to_have_count(1)
    assert console_errors == []


def _panel_order(page: Page) -> list[str]:
    return page.locator(".dl-panel-item-header").evaluate_all(
        "els => els.map(el => el.querySelector('.dl-panel-drag-handle').getAttribute('data-panel-name'))"
    )


def _wait_until(get_actual: Callable[[], list[str]], expected: list[str]) -> None:
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
    page.get_by_role("link", name="Open experiment").click()

    for panel_name in ("alpha", "beta", "gamma"):
        page.locator(f"#{NEW_PANEL_NAME_ID}").fill(panel_name)
        page.locator(f"#{NEW_PANEL_ID}").click()
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


def _ungrouped_chart_columns(experiment_id: int) -> list[str]:
    """
    The "Ungrouped" panel's charts' `column` params, in order -- reads the persisted page directly,
    since a chart's rendered `data-chart-index` alone can't distinguish *which* chart (they're
    always contiguous 0..N-1 post-render) moved where. Safe to call off the request thread: unlike
    a callback, `get_data_store()` falls back to Dash's module-global `APP` when there's no active
    callback context, and `dltrack_app` is the only app this test session ever builds.
    """
    store = get_data_store()
    page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    panel = next(p for p in page.panels if p.name == "Ungrouped")
    return [str(c.parameters["column"]) for c in panel.charts]


def test_drag_and_drop_reorders_charts_within_a_panel(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Same proof as `test_drag_and_drop_reorders_panels`, one level down: dragging a chart's handle
    onto a sibling chart in the same panel reorders just those two, via `CHART_REORDER_STORE_ID`.
    """
    _create_project_and_experiment(page, live_server_url, "Drag Reorder Charts Experiment")
    page.get_by_role("link", name="Open experiment").click()

    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDltrackAPI(live_server_url)
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
    page.get_by_role("button", name="Auto-generate charts").click()
    # No explicit `open_panel` setting exists yet for a brand new experiment, so
    # `BasicExperimentPage.render()`'s own fallback opens the first (and here, only) panel by
    # default -- no click needed for its charts (and their drag handles) to render.
    expect(page.locator(".dl-chart-drag-handle")).to_have_count(3)
    assert _ungrouped_chart_columns(experiment_id) == ["accuracy", "loss", "lr"]

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
    _wait_until(lambda: _ungrouped_chart_columns(experiment_id), ["lr", "accuracy", "loss"])
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
    page.get_by_role("link", name="Open experiment").click()

    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDltrackAPI(live_server_url)
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
    page.get_by_role("button", name="Auto-generate charts").click()
    # No explicit `open_panel` setting exists yet for a brand new experiment, so
    # `BasicExperimentPage.render()`'s own fallback already opens the first panel ("train" --
    # metrics dict insertion order is preserved through to panel-creation order) -- only "val"
    # needs a click.
    page.get_by_role("button", name="val", exact=True).click()
    expect(page.locator(".dl-chart-drag-handle")).to_have_count(2)
    assert _panel_chart_columns(experiment_id, "train") == ["train/loss"]
    assert _panel_chart_columns(experiment_id, "val") == ["val/loss"]

    # Create the empty "extra" panel *before* any drag -- a completed drop triggers its own
    # full-page re-render (a separate response from the `drag_to()` call that requested it), and
    # doing this fill()/click() while that's still in flight can land on a stale, about-to-be-
    # replaced input node. Doing it up front instead sidesteps that race entirely.
    page.locator(f"#{NEW_PANEL_NAME_ID}").fill("extra")
    page.locator(f"#{NEW_PANEL_ID}").click()
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

    _wait_until(lambda: _panel_chart_columns(experiment_id, "val"), ["train/loss", "val/loss"])
    assert _panel_chart_columns(experiment_id, "train") == []
    # `_wait_until` only confirms the backend has the move -- the drop's own re-render can still be
    # in flight. Wait for the DOM to catch up before starting the next drag on top of it.
    expect(page.locator(".dl-chart-drag-handle")).to_have_count(2)

    # Drop onto "extra"'s (empty) body -- appends, since there's no chart there to land next to.
    source = page.locator('.dl-chart-drag-handle[data-panel-name="val"][data-chart-index="0"]')
    target = page.locator('.dl-panel-body[data-panel-name="extra"]')
    source.hover()
    page.wait_for_timeout(200)
    source.drag_to(target)

    _wait_until(lambda: _panel_chart_columns(experiment_id, "extra"), ["train/loss"])
    assert _panel_chart_columns(experiment_id, "val") == ["val/loss"]
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
    not `console.error`) and that switching the grid's page size doesn't leave a phantom empty row.
    """
    _create_project_and_experiment(page, live_server_url, "Columns Picker Experiment")
    page.get_by_role("link", name="Open experiment").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])

    api = BasicDltrackAPI(live_server_url)
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
    page.on("console", lambda msg: ag_grid_warnings.append(msg.text) if "AG Grid" in msg.text else None)

    grid = page.locator(f"#{NAVBAR_HPARAM_DATATABLE_ID}")
    expect(grid.locator(".ag-center-cols-container .ag-row")).to_have_count(2)

    page.locator(f"#{NAVBAR_HPARAM_COLUMNS_TOGGLE_ID}").click()
    page.locator(f"#{NAVBAR_HPARAM_COL_SELECT_ID}").click()
    page.get_by_role("option", name="accuracy").click()

    apply_button = page.locator(f"#{NAVBAR_HPARAM_CONFIRM_COLS_ID}")
    expect(apply_button).to_be_visible()
    apply_button.click()

    expect(grid.get_by_role("columnheader", name="accuracy")).to_be_visible()

    # Changing the page size must not leave a stray extra row -- the count stays exactly 2.
    page.locator(".ag-picker-field").click()
    page.get_by_role("option", name="50").click()
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
    page.get_by_role("link", name="Open experiment").click()

    for panel_name in ("keep", "tabbed"):
        page.locator(f"#{NEW_PANEL_NAME_ID}").fill(panel_name)
        page.locator(f"#{NEW_PANEL_ID}").click()
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


def _panel_chart_columns(experiment_id: int, panel_name: str) -> list[str]:
    """Like `_ungrouped_chart_columns`, but for any named panel."""
    store = get_data_store()
    page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    panel = next(p for p in page.panels if p.name == panel_name)
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
    page.get_by_role("link", name="Open experiment").click()

    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
    api = BasicDltrackAPI(live_server_url)
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
    page.get_by_role("button", name="Auto-generate charts").click()

    # Create "Images" without picking any panel -- the picker stays empty.
    page.locator(f"#{NEW_TAB_BUTTON_ID}").click()
    page.get_by_role("dialog").get_by_role("textbox").first.fill("Images")
    page.get_by_role("button", name="Create", exact=True).click()

    images_tab = page.get_by_role("tab", name="Images")
    general_tab = page.get_by_role("tab", name="General")
    expect(images_tab).to_be_visible()
    # A real, empty panel exists in the new tab -- not just an empty tab with nothing to drop into.
    expect(page.get_by_role("button", name="Images", exact=True)).to_be_visible()
    assert _panel_chart_columns(experiment_id, "Images") == []

    # Drag the "loss" chart (in "Ungrouped", on "General") onto the "Images" tab.
    general_tab.click()
    # "Ungrouped" is already open -- it was the first (and, at auto-generate time, only) panel, so
    # `BasicExperimentPage.render()`'s fallback opened it by default; no click needed.
    handle = page.locator('.dl-chart-drag-handle[data-panel-name="Ungrouped"][data-chart-index="0"]')
    handle.hover()
    page.wait_for_timeout(200)  # matches the other drag tests' wait for the hover-reveal transition
    handle.drag_to(images_tab)

    _wait_until(lambda: _panel_chart_columns(experiment_id, "Images"), ["loss"])
    assert _panel_chart_columns(experiment_id, "Ungrouped") == []
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
    page.get_by_role("link", name="Open experiment").click()

    page.locator(f"#{NEW_PANEL_NAME_ID}").fill("only-panel")
    page.locator(f"#{NEW_PANEL_ID}").click()
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


def test_panel_area_layout_is_not_squeezed_by_the_new_panel_controls(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """
    Geometry-based regression check for a real layout bug: the "New Panel Name" input/tab-
    management controls used to live in their own container placed *beside* the accordion/tabs
    tree (a flex sibling), which visually squeezed that whole tree into sharing a row with it --
    never full width again -- and left a dead-space gap next to the title/description row above
    it. A role/text-based test can't catch that kind of thing (every control is still present and
    labelled correctly; only its position and the container's width are wrong), so this measures
    actual bounding boxes instead: the accordion/tabs container must span (nearly) the page's full
    content width, the "New Panel Name" input must sit on the very same row as the tab bar (not a
    separate row below it), and the tab-management buttons ("+"/rename) must sit right next to the
    tabs themselves, not clear across the row next to the panel-name input.
    """
    _create_project_and_experiment(page, live_server_url, "Layout Regression Experiment")
    page.get_by_role("link", name="Open experiment").click()

    for panel_name in ("keep", "tabbed"):
        page.locator(f"#{NEW_PANEL_NAME_ID}").fill(panel_name)
        page.locator(f"#{NEW_PANEL_ID}").click()
        expect(page.get_by_role("button", name=panel_name)).to_be_visible()

    # Tab the second panel so a real tab bar (not just the lone "+") is on screen too.
    page.locator(f"#{NEW_TAB_BUTTON_ID}").click()
    page.get_by_role("dialog").get_by_role("textbox").first.fill("Images")
    page.locator(f"#{NEW_TAB_PANELS_SELECT_ID}").click()
    page.get_by_role("option", name="tabbed").click()
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Create", exact=True).click()
    images_tab = page.get_by_role("tab", name="Images")
    expect(images_tab).to_be_visible()

    page_box = page.locator(f"#{PAGE_EXPERIMENT_ID}").bounding_box()
    metric_content_box = page.locator(f"#{METRIC_CONTENT_ID}").bounding_box()
    tab_box = images_tab.bounding_box()
    new_tab_button_box = page.locator(f"#{NEW_TAB_BUTTON_ID}").bounding_box()
    new_panel_input_box = page.locator(f"#{NEW_PANEL_NAME_ID}").bounding_box()
    assert page_box is not None
    assert metric_content_box is not None
    assert tab_box is not None
    assert new_tab_button_box is not None
    assert new_panel_input_box is not None

    # The accordion/tabs container spans (almost) the page's full content width -- not squeezed
    # into sharing a row with a sibling column.
    assert metric_content_box["width"] >= page_box["width"] * 0.9

    # The "+" new-tab button sits right next to the tabs themselves (a few tab-widths away at
    # most), not clear across the row next to the "New Panel Name" input.
    assert new_tab_button_box["x"] - (tab_box["x"] + tab_box["width"]) < 150

    # The "New Panel Name" input is on the very same row as the tab bar (same vertical center,
    # within a few px), not a separate row below a dead-space gap.
    tab_center_y = tab_box["y"] + tab_box["height"] / 2
    input_center_y = new_panel_input_box["y"] + new_panel_input_box["height"] / 2
    assert abs(tab_center_y - input_center_y) < 10

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
    page.get_by_role("link", name="Open experiment").click()

    for panel_name in ("keep", "tabbed"):
        page.locator(f"#{NEW_PANEL_NAME_ID}").fill(panel_name)
        page.locator(f"#{NEW_PANEL_ID}").click()
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
    and since a closest value isn't necessarily *from* the hovered x, the tooltip label says so
    (e.g. "step: 4 (2@step=3)") whenever a shown value was filled in from elsewhere.
    """
    _create_project_and_experiment(page, live_server_url, "Tooltip Consistency Experiment")
    page.get_by_role("link", name="Open experiment").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])

    api = BasicDltrackAPI(live_server_url)
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
    page.get_by_role("button", name="Auto-generate charts").click()
    # No explicit `open_panel` setting exists yet for a brand new experiment, so
    # `BasicExperimentPage.render()`'s own fallback opens the first (and here, only) panel by
    # default -- no click needed to see it render.

    svg = page.locator(".dl-panel-body svg").first
    expect(svg).to_be_visible()
    box = svg.bounding_box()
    assert box is not None

    # `LineChart.render()` labels each series "Run <run_id>" (`line_chart.py`), not the run's name.
    expected_series = {f"Run {run_a.id}", f"Run {run_b.id}"}

    # Both runs' data spans this entire x range (steps 0-9) -- every position across it must show
    # both series, not just whichever run happened to log that exact step.
    saw_a_source_annotation = False
    for frac in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
        x = box["x"] + box["width"] * frac
        y = box["y"] + box["height"] * 0.5
        # Two moves, not one -- Recharts only recomputes the hovered index on a real position
        # change, and the cursor may already be sitting at this exact pixel from a previous frac.
        page.mouse.move(x - 1, y)
        page.mouse.move(x, y)
        page.wait_for_timeout(150)
        shown = set(page.locator(".mantine-ChartTooltip-tooltipItemName").all_text_contents())
        assert shown == expected_series, (
            f"at x-fraction {frac}: tooltip showed {shown}, expected {expected_series}"
        )

        # Run B only logged every third step, so most hovered positions show *its* value filled in
        # from a nearby step, not the exact hovered one -- the label must say so (e.g.
        # "step: 4 (2@step=3)"), never implying it was logged exactly where the cursor is.
        label = page.locator(".mantine-ChartTooltip-tooltipLabel").text_content() or ""
        if "@step=" in label:
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
    dltrack, with the browser tab never touched -- must show up in the navbar run-comparison table
    on its own, with no `page.reload()`. It must also do so *without* remounting the panel: this
    tags the "Ungrouped" panel's own content node (`.dl-panel-body`, the same DOM node
    `poll_for_updates` patches via individual `chart_content_id(...)` nodes inside it) before the
    second run exists, then proves that exact node -- not a freshly rendered lookalike -- is still
    there afterward, the same "still the same node" proof
    `test_switching_tabs_does_not_remount_the_navbar_run_table` uses for the sibling flicker
    regression this feature was built to avoid.
    """
    _create_project_and_experiment(page, live_server_url, "Live Update Experiment")
    page.get_by_role("link", name="Open experiment").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])

    api = BasicDltrackAPI(live_server_url)
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
    page.get_by_role("button", name="Auto-generate charts").click()
    # No explicit `open_panel` setting exists yet for a brand new experiment, so
    # `BasicExperimentPage.render()`'s own fallback already opened "Ungrouped" by default -- the
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

    # The poll interval is 2s in this test session (`browser_test.py`'s `dltrack_app` fixture) --
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
    page.get_by_role("link", name="Open experiment").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])

    api = BasicDltrackAPI(live_server_url)
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
    page.get_by_role("button", name="Auto-generate charts").click()

    chart_items = page.locator(".dl-chart-item")
    expect(chart_items).to_have_count(2)
    cold_item, hot_item = chart_items.nth(0), chart_items.nth(1)
    cold_item.evaluate("el => el.setAttribute('data-marker', 'untouched')")
    hot_item.evaluate("el => el.setAttribute('data-marker', 'untouched')")

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
    expect(hot_item).not_to_have_attribute("data-marker", "untouched", timeout=8000)
    assert cold_item.get_attribute("data-marker") == "untouched"
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
    page.get_by_role("link", name="Open experiment").click()
    experiment_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])

    api = BasicDltrackAPI(live_server_url)
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
