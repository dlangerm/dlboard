"""Every app in a process has the shell's own callbacks, not only the first one to serve (#449)."""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

from dlboard.conftest import dispose_stores
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.serve import _constants as constants
from dlboard.serve import app as build_app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from dash import Dash
    from flask.testing import FlaskClient


def _outputs_served(app: Dash) -> set[str]:
    """
    The outputs of the callbacks `app` tells a browser it has.

    Asked in a context of its own: Dash makes the app answering `/_dash-dependencies` the process's current app, and
    would leave it so for every later test's `get_app()`.
    """
    client: FlaskClient = app.server.test_client()
    response = contextvars.copy_context().run(client.get, "/_dash-dependencies")
    return {dependency["output"] for dependency in response.json or []}


def test_a_second_app_in_the_process_still_draws_the_navbar_and_breadcrumbs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dash hands its global callbacks to whichever app serves first, which left a test's second app without them."""
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    apps = [build_app([sqlite, filesystem, *LOCAL_AUTH, *BUILTIN_BACKEND]) for _ in range(2)]
    try:
        served = [_outputs_served(app) for app in apps]
    finally:
        for app in apps:
            dispose_stores(app)

    for outputs in served:
        assert f"{constants.PAGE_BREADCRUMB_ID}.children" in outputs
