"""
End-to-end: `/healthz`/`/readyz` answer without authenticating, and `/readyz` reflects a broken store.

Spans `serve/app.py` (where these get registered), `serve/_backend/_health.py`, and a storage
plugin -- drives a real app through Flask's test client rather than the view functions in isolation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from dash import get_app

from dlboard.conftest import dispose_stores
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.serve import app as build_app
from dlboard.serve import get_system_data_store

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from flask.testing import FlaskClient

    from dlboard.models import DataStore


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FlaskClient]:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    app = build_app([sqlite, filesystem, *LOCAL_AUTH, *BUILTIN_BACKEND])
    yield app.server.test_client()
    dispose_stores(app)


def test_healthz_is_ok_with_no_credentials(client: FlaskClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_readyz_is_ok_when_the_store_answers(client: FlaskClient) -> None:
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_readyz_is_unavailable_when_the_store_is_broken(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store: DataStore[...] = get_system_data_store(get_app())

    def _broken(*_args: object, **_kwargs: object) -> None:
        msg = "db is down"
        raise RuntimeError(msg)

    monkeypatch.setattr(store, "get_projects", _broken)

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.get_json() == {"status": "unavailable"}
