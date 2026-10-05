# pyright: reportPrivateUsage=false
"""Tests for the REST client's request shaping (no real HTTP calls)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dltrack import models
from dltrack._wire import Resource
from dltrack.client import _rest_api as backend

if TYPE_CHECKING:
    import pytest


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

    def fake_post(_url: str, *, json: dict[str, Any], headers: dict[str, str] | None = None) -> _FakeResponse:
        captured["headers"] = headers
        return _FakeResponse()

    monkeypatch.setattr(backend.requests, "post", fake_post)

    backend.BasicDltrackAPI().create_project(models.NewProject(name="p", description="d"))

    assert captured["headers"][backend.DLTRACK_USER_HEADER] == "alice"
