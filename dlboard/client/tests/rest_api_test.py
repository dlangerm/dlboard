# pyright: reportPrivateUsage=false
"""Tests for the REST client's request shaping (no real HTTP calls)."""

from __future__ import annotations

import contextlib
import warnings
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import requests
from pydantic import SecretStr

from dlboard import models
from dlboard._wire import Resource
from dlboard.client import _rest_api as backend


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

    api = backend.BasicDlboardAPI()

    assert api._headers[backend.DLBOARD_USER_HEADER] == "alice"


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

    api = backend.BasicDlboardAPI()
    monkeypatch.setattr(api._session, "post", fake_post)

    api.create_project(models.NewProject(name="p", description="d"))

    assert captured["headers"][backend.DLBOARD_USER_HEADER] == "alice"


def test_an_api_key_over_plain_http_to_a_non_loopback_host_warns() -> None:
    with pytest.warns(UserWarning, match="plain HTTP"):
        backend.BasicDlboardAPI(base_url="http://example.com:8050", api_key=SecretStr("dlb_x"))


def test_an_api_key_over_https_does_not_warn() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        backend.BasicDlboardAPI(base_url="https://example.com:8050", api_key=SecretStr("dlb_x"))


def test_an_api_key_over_plain_http_to_loopback_does_not_warn() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        backend.BasicDlboardAPI(base_url="http://localhost:8050", api_key=SecretStr("dlb_x"))


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

    api = backend.BasicDlboardAPI()
    monkeypatch.setattr(api._session, "get", fake_get)

    api.whoami()

    assert captured["timeout"] == (
        backend.ClientTimeoutSettings().connect_timeout_s,
        backend.ClientTimeoutSettings().read_timeout_s,
    )


class _Reply:
    """A bare `requests.Response` stand-in: a status, and `raise_for_status` for 4xx/5xx."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=cast("requests.Response", self))

    def json(self) -> dict[str, Any]:
        return {"id": 7, "experiment_id": 1, "created_at": "2026-01-01T00:00:00+00:00", "status": "failed"}


@pytest.mark.parametrize(
    ("status_code", "raises"),
    [
        pytest.param(200, False, id="recorded"),
        pytest.param(404, False, id="a-server-without-the-route-or-a-run-that-is-gone"),
        pytest.param(500, True, id="a-real-failure"),
    ],
)
def test_finish_run_reports_how_a_run_ended_and_tolerates_a_server_that_cannot_record_it(
    monkeypatch: pytest.MonkeyPatch, status_code: int, *, raises: bool
) -> None:
    api = backend.BasicDlboardAPI(base_url="http://host:1")
    sent: dict[str, Any] = {}

    def _post(url: str, **kwargs: Any) -> _Reply:  # noqa: ANN401
        sent.update(url=url, json=kwargs["json"])
        return _Reply(status_code)

    monkeypatch.setattr(api._session, "post", _post)

    with pytest.raises(requests.HTTPError) if raises else contextlib.nullcontext():
        api.finish_run(7, models.RunStatus.FAILED)

    assert sent["url"] == "http://host:1/api/v1/runs/7/finish"
    assert sent["json"]["status"] == "failed"
    assert sent["json"]["ended_at"]  # stamped by this machine, like the run's created_at


def test_finish_run_sends_the_end_time_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """For a run being backfilled or imported, whose end is not "now"."""
    api = backend.BasicDlboardAPI(base_url="http://host:1")
    sent: dict[str, Any] = {}

    def _post(url: str, **kwargs: Any) -> _Reply:  # noqa: ANN401
        sent.update(url=url, json=kwargs["json"])
        return _Reply(200)

    monkeypatch.setattr(api._session, "post", _post)

    api.finish_run(7, models.RunStatus.FINISHED, ended_at=datetime(2026, 1, 1, 12, 30, tzinfo=UTC))

    assert sent["json"] == {"status": "finished", "ended_at": "2026-01-01T12:30:00Z"}
