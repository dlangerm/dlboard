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

import threading
import time
from typing import TYPE_CHECKING

import pendulum
import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from dltrack import models
from dltrack.plugins import LOCAL_DEPLOYMENT, themes
from dltrack.plugins.backend.basic_rest_backend import BasicDltrackAPI
from dltrack.plugins.pages.experiment import (
    NEW_PANEL_ID,
    NEW_PANEL_NAME_ID,
    PAGE_EXPERIMENT_ID,
    BasicExperimentPage,
)
from dltrack.plugins.pages.simple_homepage import NEW_PROJECT_BUTTON_ID, NEW_PROJECT_NAME_ID
from dltrack.plugins.pages.simple_project_page import NEW_EXP_BUTTON_ID, NEW_EXP_NAME_ID
from dltrack.serve import app as build_app
from dltrack.serve import get_data_store

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from dash import Dash
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser


@pytest.fixture(scope="session")
def dltrack_app(tmp_path_factory: pytest.TempPathFactory) -> Dash:
    """
    A fully wired dltrack app (the `dltrack serve local` plugin bundle) backed by a throwaway sqlite db.

    Session-scoped, matching how the real app runs: exactly one process builds it once. Chart
    plugins register their `ChartType` into a process-global registry on `plug()` and reject a
    second registration of the same name, so building the app more than once per process (e.g. a
    fresh one per test) isn't safe here the way it is for the isolated unit tests.
    """
    tmp_path = tmp_path_factory.mktemp("dltrack-browser")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("SQLITE_LOCATION", str(tmp_path / "test.sqlite"))
    monkeypatch.setenv("ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    try:
        return build_app([*LOCAL_DEPLOYMENT, themes.dark])
    finally:
        monkeypatch.undo()


@pytest.fixture(scope="session")
def live_server_url(dltrack_app: Dash) -> Iterator[str]:
    """Serve `dltrack_app` on a background thread for the duration of the test session."""
    server = make_server("127.0.0.1", 0, dltrack_app.server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


@pytest.fixture
def console_errors(page: Page) -> list[str]:
    """Collect any browser console errors raised while the test runs (e.g. a failed callback)."""
    errors: list[str] = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    return errors


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

    expect(page.locator(f"#{PAGE_EXPERIMENT_ID} svg")).to_be_visible()
    assert console_errors == []


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

    # Both panels start closed (this experiment's accordion already persisted an empty
    # `open_panel` before either panel existed). Round-trip the control button itself: opening,
    # collapsing, then re-expanding must all still work with the extra wrapping Group in place.
    # Every panel header (open or closed) carries its own hover-revealed "+", so that's no longer
    # a usable open/closed signal -- and `.dl-panel-body` itself collapses to zero height once
    # "keep" (a brand new, chart-less panel) has no content, so it's not reliable either. Mantine's
    # own collapsible region (`value="keep"` on the `AccordionItem`) is: its height/aria-hidden
    # genuinely track open/closed regardless of what's inside.
    keep_region = page.locator("#experiment-accordion-panel-keep")
    keep_control = page.get_by_role("button", name="keep")
    keep_control.click()
    expect(keep_region).to_be_visible()
    keep_control.click()
    expect(keep_region).not_to_be_visible()
    keep_control.click()
    expect(keep_region).to_be_visible()

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
    expect(keep_region).to_be_visible()  # "keep" is untouched by deleting its sibling
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
    # The new "Ungrouped" panel starts collapsed (`open_panel` was already persisted as `[]` before
    # any panel existed) -- charts (and their drag handles) only render once it's open.
    page.get_by_role("button", name="Ungrouped").click()
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
