"""End-to-end: an artifact upload the server can't take right now is a retryable 503, not a hang or a 500."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest
from dash import get_app

from dlboard.conftest import dispose_stores
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.plugins.data_stores._blob_store import BlobArtifactStore
from dlboard.serve import app as build_app
from dlboard.serve import get_system_artifact_store

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from flask.testing import FlaskClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FlaskClient]:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    app = build_app([sqlite, filesystem, *LOCAL_AUTH, *BUILTIN_BACKEND])
    yield app.server.test_client()
    dispose_stores(app)


def test_upload_is_a_503_with_retry_after_when_the_write_worker_has_died(client: FlaskClient) -> None:
    store = get_system_artifact_store(get_app())
    assert isinstance(store, BlobArtifactStore)
    store.dispose()

    response = client.post(
        "/api/v1/artifacts",
        data={"a": (io.BytesIO(b"x"), "a.png")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"]
