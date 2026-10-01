"""Scopes: the set of grantable permissions for destructive/admin actions."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dltrack.models._user import User


class Scope(StrEnum):
    """
    A single grantable permission.

    A closed, typed set rather than bare strings so every call site gets autocomplete and
    static-analysis coverage instead of scattering string literals. `ALL` is a wildcard matching
    every scope, including ones added in the future -- it's what the local single-user bootstrap
    admin gets (see `SQLStoreBase`), not something granted by default to every user.
    """

    ALL = "*"
    PROJECT_DELETE = "project:delete"
    EXPERIMENT_DELETE = "experiment:delete"
    RUN_DELETE = "run:delete"
    ARTIFACT_DELETE = "artifact:delete"
    RESTORE = "restore"
    PURGE = "purge"
    AUDIT_LOG_READ = "audit_log:read"
    USER_MANAGE = "user:manage"


def has_scope(user: User, scope: Scope) -> bool:
    """Whether `user` has been granted `scope`, directly or via the `Scope.ALL` wildcard."""
    return scope in user.scopes or Scope.ALL in user.scopes


def require_scope(user: User, scope: Scope) -> None:
    """Raise `PermissionError` if `user` lacks `scope`. The single enforcement point every `DataStore` uses."""
    if not has_scope(user, scope):
        msg = f"User {user.id} lacks the {scope} scope"
        raise PermissionError(msg)
