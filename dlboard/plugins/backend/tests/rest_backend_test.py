"""Tests for route registration and the error -> HTTP status mappings (no real HTTP calls)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import dash
import pytest
from pydantic import ValidationError

from dlboard import models
from dlboard.models import PinColor, ProjectRole
from dlboard.plugins.backend import basic_rest_backend as backend
from dlboard.serve._backend._authorization import AuthorizingDataStore

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


def test_permission_error_maps_to_a_403() -> None:
    body, status = backend._handle_permission_error(PermissionError("User 1 lacks the purge scope"))

    assert status == 403
    assert body == {"error": "User 1 lacks the purge scope"}


def test_importing_this_module_registers_nothing_in_dash_hooks_global_registry() -> None:
    """This module (imported eagerly by `dlboard.plugins`, to build its bundles, whether or not any
    app ever uses it) must never register routes as a side effect of import -- only `plug()`,
    called with a specific `app`, may do that. Otherwise a process building more than one `Dash`
    app would double-register them."""
    registered_paths: set[str] = {
        h.data["name"]  # pyrefly: ignore [unsupported-operation]
        for h in dash.hooks.get_hooks("routes")
    }
    route_paths = {path for path, _methods, _view_func in backend._ROUTES}

    assert not (registered_paths & route_paths)


def test_plug_registers_routes_only_on_the_given_app_not_globally() -> None:
    """Routes must attach to the one `app.server` passed to `plug()`, not some global registry --
    otherwise merely importing this module or building more than one `Dash` app in a process would
    leak or duplicate route registrations."""
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

    backend.plug(_FakeApp())  # pyrefly: ignore [bad-argument-type]

    assert len(registered_rules) == len(backend._ROUTES)
    assert registered_errorhandlers == {
        PermissionError: backend._handle_permission_error,
        ValidationError: backend._handle_validation_error,
        models.UnservableArtifactRefError: backend._handle_unservable_ref_error,
        models.AmbiguousProjectError: backend._handle_ambiguous_project,
        models.ProjectLockedError: backend._handle_project_locked,
        models.ArtifactStoreUnavailableError: backend._handle_store_unavailable,
    }


def test_a_project_that_takes_no_new_runs_maps_to_a_423_the_client_does_not_retry() -> None:
    body, status = backend._handle_project_locked(models.ProjectLockedError("takes no new runs"))

    assert (status, body) == (423, {"error": "takes no new runs"})


def test_a_viewer_who_may_not_keep_a_run_has_not_pinned_it_either(store: SQLLiteStore) -> None:
    """Keeping needs an editor and pinning only a viewer, so the keep is asked first and a refusal marks nothing."""
    alice = store.get_or_create_user(models.Principal(issuer="test", subject="alice", username="alice"))
    bob = store.get_or_create_user(models.Principal(issuer="test", subject="bob", username="bob"))
    project = AuthorizingDataStore(store, alice, enforce_grants=True).create_project(
        models.NewProject(name="p", description="")
    )
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    run = store.create_run(models.NewRun(experiment_id=experiment.id))
    grant = models.NewProjectGrant(project_id=project.id, user_id=bob.id, role=ProjectRole.VIEWER)
    store.set_project_grant(grant)

    as_bob = AuthorizingDataStore(store, bob, enforce_grants=True)
    with pytest.raises(PermissionError):
        backend.handle_mark_run(as_bob, run.id, {"keep": True, "pin": PinColor.RED}, bob)

    assert store.kept_runs(experiment.id, run_ids=None) == frozenset()
    assert store.pins_of(experiment.id, run_ids=None) == {}
    # A viewer may pin what they see, which is all this asks for.
    backend.handle_mark_run(as_bob, run.id, {"pin": PinColor.RED}, bob)
    assert list(store.pins_of(experiment.id, run_ids=None)) == [run.id]
