"""
Browser-driven end-to-end regression tests for the composed dltrack app.

Exercises the app the way a real deployment is wired -- `dltrack.serve.app.app()` with the same
plugin bundle `serve.py` uses -- through an actual rendered browser (Playwright). A real browser
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
from typing import TYPE_CHECKING

import pendulum
import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from dltrack import models
from dltrack.models import constants
from dltrack.plugins import LOCAL_DEPLOYMENT, themes
from dltrack.plugins.backend.basic_rest_backend import BasicDltrackAPI
from dltrack.plugins.pages.simple_homepage import NEW_PROJECT_BUTTON_ID, NEW_PROJECT_NAME_ID
from dltrack.plugins.pages.simple_project_page import NEW_EXP_BUTTON_ID, NEW_EXP_NAME_ID
from dltrack.serve import app as build_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dash import Dash
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser


@pytest.fixture(scope="session")
def dltrack_app(tmp_path_factory: pytest.TempPathFactory) -> Dash:
    """
    A fully wired dltrack app (the `serve.py` plugin bundle) backed by a throwaway sqlite db.

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
    JS chart library drew real SVG from it in a browser, after a *clientside* (JS-only) panel-drawer
    toggle and the "Auto-generate charts" action -- none of which a Python-level test can see.
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
    page.get_by_role("button", name="Manage panels").click()
    page.get_by_role("button", name="Auto-generate charts").click()

    expect(page.locator(f"#{constants.PAGE_EXPERIMENT_ID} svg")).to_be_visible()
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
