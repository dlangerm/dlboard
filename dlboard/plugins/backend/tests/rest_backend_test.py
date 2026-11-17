"""Tests for route registration and the error -> HTTP status mappings (no real HTTP calls)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import dash
import pytest

from dlboard import models
from dlboard.models import PinColor, ProjectRole
from dlboard.plugins.backend import basic_rest_backend as backend
from dlboard.serve._backend._authorization import AuthorizingDataStore

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


@pytest.mark.parametrize(
    ("error", "status", "headers"),
    [
        (PermissionError("User 1 lacks the purge scope"), 403, {}),
        (models.UnservableArtifactRefError("not ours"), 400, {}),
        # Regression: a run deleted mid-training was a 500, which a client retries forever.
        (models.EntityGoneError("Run 7 has been deleted"), 404, {}),
        (models.AmbiguousProjectError("two named p"), 409, {}),
        # A 4xx: the client does not retry it, and says why.
        (models.ProjectLockedError("takes no new runs"), 423, {}),
        # A 5xx, so the client retries it, when the server says.
        (models.ArtifactStoreUnavailableError("busy"), 503, {"Retry-After": "5"}),
    ],
)
def test_each_refusal_maps_to_the_status_that_tells_a_client_whether_to_retry(
    error: Exception, status: int, headers: dict[str, str]
) -> None:
    assert backend._error_response(error) == ({"error": str(error)}, status, headers)


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
        kind: backend._error_response for kind, _status in backend._ERROR_STATUSES
    }


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
