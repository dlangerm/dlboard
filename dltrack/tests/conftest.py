"""Fixtures shared by the browser-driven tests: one composed app, served on a real socket."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest
from werkzeug.serving import make_server

from dltrack.plugins import LOCAL_DEPLOYMENT_DEFAULT
from dltrack.serve import app as build_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dash import Dash
    from playwright.sync_api import Page


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
    # `LiveUpdateSettings` is instantiated once, at `serve/_pages/experiment.py`'s own first
    # import -- which (via `app.py`'s deferred page import) happens inside this very `build_app`
    # call, so this env var only has to be set before that, not for the whole test session. Down
    # from its 30s production default so the live-update browser tests don't need multi-poll
    # patience just to observe one tick.
    monkeypatch.setenv("POLL_INTERVAL_MS", "2000")
    try:
        app = build_app(LOCAL_DEPLOYMENT_DEFAULT)
    finally:
        monkeypatch.undo()

    # Dash's callback-graph checks (circular dependencies, two callbacks writing to the same/an
    # overlapping output without `allow_duplicate=True`, a pattern-matching Output/Input mismatch,
    # ...) run client-side in dash-renderer, only when the server tells it to -- serving via a
    # plain `werkzeug` server the way `live_server_url` does (rather than `app.run(debug=...)`)
    # skips that entirely by default, same as a production deployment does. Turning it on here
    # means this suite -- the one place callbacks actually run end to end -- catches that whole
    # class of wiring mistake, not just a developer happening to notice it running `dltrack serve
    # local` in debug mode. `dev_tools_hot_reload`/`dev_tools_ui` are off -- neither is needed to
    # trigger the validation itself, and hot-reload's periodic polling and the dev-tools UI's own
    # DOM footprint are both pure noise (or, for the layout-geometry tests, actively
    # counterproductive) in a test session.
    app.enable_dev_tools(dev_tools_hot_reload=False, dev_tools_ui=False)
    return app


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
