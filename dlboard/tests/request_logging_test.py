"""
End-to-end: every response carries a request id, and the access log is off unless opted into.

Spans `serve/app.py` (where this gets registered) and `serve/_backend/_request_logging.py` --
drives a real app through Flask's test client.
"""

from __future__ import annotations

from contextlib import contextmanager
from http import HTTPStatus
from typing import TYPE_CHECKING, NamedTuple

import pytest

from dlboard._wire import Resource, create_path
from dlboard.conftest import dispose_stores
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH, PASSWORD_AUTH
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.serve import app as build_app

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from pathlib import Path

    from dash import Dash
    from flask.testing import FlaskClient

    from dlboard.models import PluginProtocol


class _Deployment(NamedTuple):
    client: FlaskClient
    app: Dash


@contextmanager
def _deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth: list[PluginProtocol] = LOCAL_AUTH
) -> Generator[_Deployment]:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    app = build_app([sqlite, filesystem, *auth, *BUILTIN_BACKEND])
    try:
        yield _Deployment(app.server.test_client(), app)  # pyrefly: ignore [unknown-argument-type]
    finally:
        dispose_stores(app)


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Deployment]:
    with _deploy(tmp_path, monkeypatch) as deployment:
        yield deployment


def test_every_response_carries_a_request_id(deployment: _Deployment) -> None:
    response = deployment.client.get("/healthz")

    assert response.headers["X-Request-Id"]


def test_a_client_supplied_request_id_is_echoed_back(deployment: _Deployment) -> None:
    response = deployment.client.get("/healthz", headers={"X-Request-Id": "caller-supplied-id"})

    assert response.headers["X-Request-Id"] == "caller-supplied-id"


def test_access_log_is_off_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with _deploy(tmp_path, monkeypatch) as deployment:
        deployment.client.get("/healthz")

    assert "/healthz" not in capsys.readouterr().out


def test_access_log_is_emitted_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DLBOARD_ACCESS_LOG", "true")
    with _deploy(tmp_path, monkeypatch) as deployment:
        deployment.client.get("/healthz")

    output = capsys.readouterr().out
    assert "/healthz" in output
    assert "GET" in output


def test_a_request_the_sign_in_gate_turns_away_is_still_logged_with_its_request_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DLBOARD_ACCESS_LOG", "true")
    monkeypatch.setenv("DLBOARD_SECRET_KEY", "test-secret-at-least-32-characters-long")
    with _deploy(tmp_path, monkeypatch, PASSWORD_AUTH) as deployment:
        response = deployment.client.post(f"/{create_path(Resource.METRICS)}", json=[])

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["X-Request-Id"]
    assert "401" in capsys.readouterr().out
