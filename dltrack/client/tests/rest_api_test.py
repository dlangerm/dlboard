# pyright: reportPrivateUsage=false
"""Tests for the REST client's request shaping (no real HTTP calls)."""

from __future__ import annotations

import warnings
from typing import Any

import pytest
from pydantic import SecretStr

from dltrack import models
from dltrack._wire import Resource
from dltrack.client import _rest_api as backend


def test_create_path_joins_base_url_and_resource() -> None:
    assert backend.create_path(Resource.PROJECTS, base_url="http://host:1") == "http://host:1/api/v1/projects"


def test_entity_path_defaults_placeholder() -> None:
    assert (
        backend.entity_path(Resource.PROJECTS, base_url="http://host:1")
        == "http://host:1/api/v1/projects/<int:entity_id>"
    )


def test_entity_path_accepts_a_real_id() -> None:
    assert (
        backend.entity_path(Resource.RUNS, entity_id="7", base_url="http://host:1")
        == "http://host:1/api/v1/runs/7"
    )


def test_client_sends_resolved_username_header_on_every_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend, "resolve_username", lambda: "alice")

    api = backend.BasicDltrackAPI()

    assert api._headers[backend.DLTRACK_USER_HEADER] == "alice"


def test_create_project_sends_the_user_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend, "resolve_username", lambda: "alice")

    captured: dict[str, Any] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, Any]:
            return {
                "id": 1,
                "name": "p",
                "description": "d",
                "created_by": None,
                "created_at": "2026-01-01T00:00:00+00:00",
                "deleted_by": None,
                "deleted_at": None,
            }

    def fake_post(
        _url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> _FakeResponse:
        del timeout
        captured["headers"] = headers
        return _FakeResponse()

    api = backend.BasicDltrackAPI()
    monkeypatch.setattr(api._session, "post", fake_post)

    api.create_project(models.NewProject(name="p", description="d"))

    assert captured["headers"][backend.DLTRACK_USER_HEADER] == "alice"


def test_an_api_key_over_plain_http_to_a_non_loopback_host_warns() -> None:
    with pytest.warns(UserWarning, match="plain HTTP"):
        backend.BasicDltrackAPI(base_url="http://example.com:8050", api_key=SecretStr("dlt_x"))


def test_an_api_key_over_https_does_not_warn() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        backend.BasicDltrackAPI(base_url="https://example.com:8050", api_key=SecretStr("dlt_x"))


def test_an_api_key_over_plain_http_to_loopback_does_not_warn() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        backend.BasicDltrackAPI(base_url="http://localhost:8050", api_key=SecretStr("dlt_x"))


def test_client_sends_a_timeout_on_every_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a hung server used to block the training loop forever -- `requests` has no default."""
    captured: dict[str, Any] = {}

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, Any]:
            return {"id": 1, "username": "me", "server_version": "0.0.0", "api_version": backend.API_VERSION}

    def fake_get(_url: str, *, headers: dict[str, str] | None = None, timeout: Any = None) -> _FakeResponse:  # noqa: ANN401
        captured["timeout"] = timeout
        return _FakeResponse()

    api = backend.BasicDltrackAPI()
    monkeypatch.setattr(api._session, "get", fake_get)

    api.whoami()

    assert captured["timeout"] == (
        backend.ClientTimeoutSettings().connect_timeout_s,
        backend.ClientTimeoutSettings().read_timeout_s,
    )
