"""
End-to-end: a request is served by its own app's stores, however many apps the process has built.

Spans `serve/app.py` (which records each app on its Flask server), `_app_slot.current_dash_app` and
the REST backend. Dash scopes only its own routes and callbacks to their app; a plain Flask route
would otherwise get Dash's process-wide "last app built" -- the wrong one in any test session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dlboard._wire import GetOrCreateProject, Resource, get_or_create_path
from dlboard.conftest import dispose_stores
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.serve import app as build_app
from dlboard.serve import get_system_data_store

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from dash import Dash
    from flask.testing import FlaskClient


def _build(directory: Path, monkeypatch: pytest.MonkeyPatch) -> Dash:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(directory / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(directory / "artifacts"))
    return build_app([sqlite, filesystem, *LOCAL_AUTH, *BUILTIN_BACKEND])


def test_a_rest_request_uses_its_own_apps_store_even_after_another_app_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _build(tmp_path / "first", monkeypatch)
    second = _build(tmp_path / "second", monkeypatch)  # now the one Dash itself calls current
    try:
        client: FlaskClient = first.server.test_client()
        response = client.post(
            f"/{get_or_create_path(Resource.PROJECTS)}", json=GetOrCreateProject(name="mine").model_dump()
        )

        assert response.status_code == 200
        assert [p.name for p in get_system_data_store(first).get_projects()] == ["mine"]
        assert list(get_system_data_store(second).get_projects()) == []
    finally:
        dispose_stores(first)
        dispose_stores(second)
