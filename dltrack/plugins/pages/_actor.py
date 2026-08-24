"""
Best-effort "who is doing this" for UI-driven actions.

Dash callbacks run in-process, server-side -- there's no per-request `X-Dltrack-User` header to
read the way `basic_rest_backend.resolve_actor` does for the REST API. The best we can do without
a real session/auth model is the same best-effort chain the client uses to attribute what it logs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dltrack._identity import resolve_username

if TYPE_CHECKING:
    from dltrack.models import DataStore, User


def current_actor(store: DataStore[...]) -> User:
    """Resolve (get-or-create) the user to attribute a UI-driven destructive action to."""
    return store.get_or_create_user(resolve_username())


def current_actor_id(store: DataStore[...]) -> int:
    """Resolve (get-or-create) the user id to attribute a UI-driven destructive action to."""
    return current_actor(store).id
