"""
End-to-end: `DLBOARD_URL_PREFIX` moves every route under that prefix, and `relative_path` reflects it.

For a reverse proxy that forwards the full, un-rewritten path through (see `dlboard.serve.app.app`'s
own docstring for why this -- not a WSGI-level `SCRIPT_NAME` -- is the deployment model supported).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from flask.testing import FlaskClient

from dlboard.conftest import dispose_stores
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.serve import app as build_app
from dlboard.serve import relative_path

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from flask.testing import FlaskClient


@pytest.fixture
def prefixed_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FlaskClient]:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    app = build_app([sqlite, filesystem, *LOCAL_AUTH, *BUILTIN_BACKEND], url_prefix="dlboard")
    client: FlaskClient = app.server.test_client()
    yield client
    dispose_stores(app)


def test_every_route_moves_under_the_prefix(prefixed_client: FlaskClient) -> None:
    assert prefixed_client.get("/dlboard/healthz").status_code == 200


def test_the_route_is_not_also_served_unprefixed(prefixed_client: FlaskClient) -> None:
    assert prefixed_client.get("/healthz").status_code == 404


def test_relative_path_reflects_the_configured_prefix(prefixed_client: FlaskClient) -> None:
    del prefixed_client  # only needed to force the prefixed app to be the one currently built
    assert relative_path("/project/5") == "/dlboard/project/5"


def test_relative_path_is_unprefixed_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    app = build_app([sqlite, filesystem, *LOCAL_AUTH, *BUILTIN_BACKEND])
    try:
        assert relative_path("/project/5") == "/project/5"
    finally:
        dispose_stores(app)
