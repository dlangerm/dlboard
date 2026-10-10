"""Tests for `dlboard.serve._backend._auth`: the provider slot, `get_current_user`, and `sign_in`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum
import pytest
from dash import Dash
from flask import Flask

from dlboard import models
from dlboard.plugins.auth.anonymous import AnonymousAuthProvider
from dlboard.serve._backend import _auth

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


class _FakeApp(Dash):
    """A bare stand-in for `Dash` -- `set_auth_provider`/`get_auth_provider` only need `hasattr`/`setattr`."""

    def __init__(self) -> None:
        # Deliberately skips `Dash.__init__`: nothing here needs a real app, only an identity to hang state on.
        pass


def _principal(username: str, *groups: str) -> models.Principal:
    return models.Principal(issuer="test", subject=username, username=username, groups=frozenset(groups))


def test_set_auth_provider_refuses_to_overwrite_an_existing_one() -> None:
    app = _FakeApp()
    _auth.set_auth_provider(app, AnonymousAuthProvider.get_or_create())

    with pytest.raises(AttributeError):
        _auth.set_auth_provider(app, AnonymousAuthProvider.get_or_create())


def test_get_current_user_refuses_a_request_nobody_authenticated() -> None:
    with (
        Flask(__name__).test_request_context(),
        pytest.raises(_auth.UnauthenticatedRequestError, match="request gate"),
    ):
        _auth.get_current_user()


@pytest.mark.parametrize(
    ("principal", "is_admin"),
    [
        (_principal("root"), True),
        (_principal("someone", "platform-admins"), True),
        (_principal("someone", "ml-team"), False),
    ],
)
def test_sign_in_grants_configured_admins(
    store: SQLLiteStore, principal: models.Principal, is_admin: bool
) -> None:
    settings = _auth.AuthSettings(admin_users=["root"], admin_groups=["platform-admins"])

    user = _auth.sign_in(store, principal, settings)

    assert user is not None
    assert (models.Scope.ALL in user.scopes) is is_admin


def test_sign_in_never_makes_the_first_verified_user_an_admin(store: SQLLiteStore) -> None:
    """Whoever signs in first is only made admin locally -- never once identity is actually verified."""
    user = _auth.sign_in(store, _principal("first"), _auth.AuthSettings())

    assert user is not None
    assert user.scopes == []


def test_sign_in_refuses_a_disabled_user(store: SQLLiteStore) -> None:
    user = store.get_or_create_user(_principal("gone"))
    store.update_user(user.model_copy(update={"disabled_at": pendulum.now(pendulum.UTC)}))

    assert _auth.sign_in(store, _principal("gone"), _auth.AuthSettings()) is None


def test_sign_in_refreshes_groups_and_keeps_one_user_per_identity(store: SQLLiteStore) -> None:
    first = _auth.sign_in(store, _principal("ann", "a"), _auth.AuthSettings())
    again = _auth.sign_in(store, _principal("ann", "b"), _auth.AuthSettings())

    assert first is not None
    assert again is not None
    assert (again.id, again.groups) == (first.id, ["b"])


def test_a_username_taken_by_another_identity_is_refused(store: SQLLiteStore) -> None:
    store.get_or_create_user(models.Principal.unverified("sam"))

    with pytest.raises(ValueError, match="already taken"):
        store.get_or_create_user(_principal("sam"))


@pytest.mark.parametrize(
    ("next_path", "expected"),
    [
        ("/project/1", "/project/1"),
        ("/project/1?tab=runs#top", "/project/1?tab=runs"),
        ("//evil.example", "/"),
        ("///evil.example", "/evil.example"),
        ("/\\evil.example", "/"),
        ("https://evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("relative/path", "/"),
        ("", "/"),
        (None, "/"),
    ],
)
def test_safe_next_path_only_allows_same_site_paths(next_path: str | None, expected: str) -> None:
    assert _auth.safe_next_path(next_path) == expected
