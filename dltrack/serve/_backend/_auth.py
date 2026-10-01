"""The app's auth provider, set once by an auth plugin, and the one place identity becomes a `User`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dltrack import models
from dltrack._identity import ANONYMOUS
from dltrack.serve._backend._app_slot import AppSlot

if TYPE_CHECKING:
    from dltrack.models import AuthProvider, DataStore, User

AUTH_PROVIDER: AppSlot[AuthProvider[...]] = AppSlot("auth provider")

set_auth_provider = AUTH_PROVIDER.set
get_auth_provider = AUTH_PROVIDER.get


def get_current_user(store: DataStore[...]) -> User:
    """
    Resolve (get-or-create) the user to attribute the in-flight request/callback to.

    The single place identity gets resolved to a `User`, for both REST handlers and UI-driven
    callbacks alike -- both just need "who is this", and the registered `AuthProvider` is the only
    thing that knows how to answer that.
    """
    username = get_auth_provider().resolve_identity() or ANONYMOUS
    return store.get_or_create_user(models.Principal.unverified(username))
