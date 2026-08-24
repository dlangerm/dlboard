"""A plugin using dash hooks that exposes the current auth provider to callbacks via callback context."""

from __future__ import annotations

import time
from functools import cache
from typing import TYPE_CHECKING, Final, cast

from dash import Dash, get_app
from dash.exceptions import AppNotFoundError
from structlog.stdlib import get_logger

from dltrack._identity import ANONYMOUS

_log = get_logger(__name__)


if TYPE_CHECKING:
    from dltrack.models import AuthProvider, DataStore, User

_DLTRACK_AUTH_PROVIDER: Final = "_dltrack_auth_provider_"


def set_auth_provider(app: Dash, provider: AuthProvider[...]) -> None:
    """Set the auth provider for a dash app, use this in plugins."""
    if hasattr(app, _DLTRACK_AUTH_PROVIDER):
        msg = "Refusing to overwrite an existing auth provider."
        raise AttributeError(msg)
    _log.info("set auth provider to %s", provider.__class__.__name__)
    setattr(app, _DLTRACK_AUTH_PROVIDER, provider)


@cache
def get_auth_provider() -> AuthProvider[...]:
    """Strongly typed helper function for callbacks who need the auth provider."""
    app = cast("Dash", get_app())
    if not hasattr(app, _DLTRACK_AUTH_PROVIDER):
        msg = "Auth provider was not set for the app"
        raise AttributeError(msg)
    return cast("AuthProvider[...]", getattr(app, _DLTRACK_AUTH_PROVIDER))


def wait_for_auth_provider() -> AuthProvider[...]:
    """Like `get_auth_provider`, but retries once a second until the app exists. See `wait_for_data_store`."""
    try:
        return get_auth_provider()
    except AppNotFoundError:
        time.sleep(1)
        return wait_for_auth_provider()


def get_current_user(store: DataStore[...]) -> User:
    """
    Resolve (get-or-create) the user to attribute the in-flight request/callback to.

    The single place identity gets resolved to a `User`, for both REST handlers and UI-driven
    callbacks alike -- both just need "who is this", and the registered `AuthProvider` is the only
    thing that knows how to answer that.
    """
    username = get_auth_provider().resolve_identity() or ANONYMOUS
    return store.get_or_create_user(username)
