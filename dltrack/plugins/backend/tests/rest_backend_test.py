# pyright: reportPrivateUsage=false
"""Tests for the REST client's request shaping (no real HTTP calls)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import dash
from pydantic import ValidationError

from dltrack import models
from dltrack.plugins.backend import basic_rest_backend as backend

if TYPE_CHECKING:
    import pytest


def test_create_path_joins_base_url_and_model_name() -> None:
    assert backend.create_path(models.Project, base_url="http://host:1") == "http://host:1/create/Project"


def test_permission_error_maps_to_a_403() -> None:
    body, status = backend._handle_permission_error(PermissionError("User 1 lacks the purge scope"))

    assert status == 403
    assert body == {"error": "User 1 lacks the purge scope"}


def test_importing_this_module_registers_nothing_in_dash_hooks_global_registry() -> None:
    """This module (imported by this test file, and by other test files, and by the client) must
    never register routes as a side effect of import -- only `plug()`, called with a specific
    `app`, may do that. Otherwise anything importing this module for another reason (e.g. the
    client importing `BasicDltrackAPI`) would leak these routes into every `Dash` app in the
    process, and a process building more than one app would double-register them."""
    registered_paths: set[str] = {
        h.data["name"]  # pyright: ignore[reportUnknownMemberType,reportOptionalSubscript]
        for h in dash.hooks.get_hooks("routes")  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
    }
    route_paths = {path for path, _methods, _view_func in backend._ROUTES}

    assert not (registered_paths & route_paths)


def test_plug_registers_routes_only_on_the_given_app_not_globally() -> None:
    """Routes must attach to the one `app.server` passed to `plug()`, not some global registry --
    otherwise merely importing this module (e.g. the client importing `BasicDltrackAPI`) or
    building more than one `Dash` app in a process would leak or duplicate route registrations."""
    registered_rules: list[tuple[str, list[str]]] = []
    registered_errorhandlers: dict[type[Exception], Any] = {}

    class _FakeServer:
        def add_url_rule(
            self,
            rule: str,
            *,
            endpoint: str,
            view_func: Any,  # noqa: ANN401
            methods: list[str],
        ) -> None:
            registered_rules.append((rule, methods))

        def errorhandler(self, exc_type: type[Exception]) -> Any:  # noqa: ANN401
            def _register(func: Any) -> Any:  # noqa: ANN401
                registered_errorhandlers[exc_type] = func
                return func

            return _register

    class _FakeConfig:
        routes_pathname_prefix = "/"

    class _FakeApp:
        server = _FakeServer()
        config = _FakeConfig()

    backend.plug(_FakeApp())  # pyright: ignore[reportArgumentType]

    assert len(registered_rules) == len(backend._ROUTES)
    assert registered_errorhandlers == {
        PermissionError: backend._handle_permission_error,
        ValidationError: backend._handle_validation_error,
        models.UnservableArtifactRefError: backend._handle_unservable_ref_error,
        models.AmbiguousProjectError: backend._handle_ambiguous_project,
    }


def test_create_path_handles_no_model() -> None:
    assert backend.create_path(None, base_url="http://host:1") == "http://host:1/create"


def test_entity_path_lowercases_model_name_and_defaults_placeholder() -> None:
    assert (
        backend.entity_path(models.Project, base_url="http://host:1")
        == "http://host:1/project/<int:entity_id>"
    )


def test_entity_path_accepts_a_real_id() -> None:
    assert backend.entity_path(models.Run, entity_id="7", base_url="http://host:1") == "http://host:1/run/7"


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

    assert captured["headers"] == {backend.DLTRACK_USER_HEADER: "alice"}
