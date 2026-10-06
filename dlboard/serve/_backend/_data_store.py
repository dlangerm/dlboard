"""
The app's data store and artifact store: set once by storage plugins, read by every callback/route.

A request only ever sees them through `get_data_store()`/`get_artifact_store()`: authorizing
wrappers bound to the user the request gate authenticated (see `_authorization.py`). The raw
stores are reachable only through the `system` accessors, for work no user is behind -- the
request gate resolving who the caller is in the first place, and background workers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

from dash import get_app
from flask import g, has_request_context

from dlboard.serve._backend._app_slot import AppSlot
from dlboard.serve._backend._auth import AUTH_SETTINGS, get_auth_provider, get_current_user
from dlboard.serve._backend._authorization import AuthorizingArtifactStore, AuthorizingDataStore

if TYPE_CHECKING:
    from dash import Dash

    from dlboard.models import ArtifactStore, DataStore, ProjectRole

DATA_STORE: AppSlot[DataStore[...]] = AppSlot("data store")
ARTIFACT_STORE: AppSlot[ArtifactStore[...]] = AppSlot("artifact store")

set_data_store = DATA_STORE.set
get_system_data_store = DATA_STORE.get
wait_for_data_store = DATA_STORE.wait
set_artifact_store = ARTIFACT_STORE.set
get_system_artifact_store = ARTIFACT_STORE.get
wait_for_artifact_store = ARTIFACT_STORE.wait

_AUTHORIZER_KEY: Final = "dlboard_authorizer"


def _authorizer() -> AuthorizingDataStore:
    """The in-flight request's authorizing store, built once per request."""
    if not has_request_context():
        msg = "get_data_store() is per-request; work no user is behind uses get_system_data_store()"
        raise RuntimeError(msg)
    authorizer: AuthorizingDataStore | None = g.get(_AUTHORIZER_KEY)
    if authorizer is None:
        app = cast("Dash", get_app())
        authorizer = AuthorizingDataStore(
            DATA_STORE.get(app),
            get_current_user(),
            enforce_grants=get_auth_provider(app).verifies_identity,
            new_project_access=AUTH_SETTINGS.get(app).new_project_access,
        )
        setattr(g, _AUTHORIZER_KEY, authorizer)
    return authorizer


def get_data_store() -> DataStore[...]:
    """The data store, as the in-flight request's user is allowed to use it."""
    return _authorizer()


def get_artifact_store() -> ArtifactStore[...]:
    """The artifact store, as the in-flight request's user is allowed to use it."""
    app = cast("Dash", get_app())
    return AuthorizingArtifactStore(ARTIFACT_STORE.get(app), _authorizer())


def get_project_role(project_id: int) -> ProjectRole | None:
    """The in-flight request's user's effective role on `project_id` -- for showing or hiding controls."""
    return _authorizer().role_on(project_id)
