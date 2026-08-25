# pyright: reportPrivateUsage=false
"""Tests for `dltrack.serve._backend._auth`: the auth-provider accessor and `get_current_user`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dltrack.serve._backend import _auth

if TYPE_CHECKING:
    from dltrack.plugins.data_stores.sqlite import SQLLiteStore


class _FakeAuthProvider:
    def __init__(self, identity: str | None) -> None:
        self._identity = identity

    def resolve_identity(self) -> str | None:
        return self._identity

    @classmethod
    def get_or_create(cls, identity: str | None) -> _FakeAuthProvider:
        return cls(identity)


class _FakeApp:
    """A bare stand-in for `Dash` -- `set_auth_provider`/`get_auth_provider` only need `hasattr`/`setattr`."""


def test_set_auth_provider_refuses_to_overwrite_an_existing_one() -> None:
    app = _FakeApp()
    _auth.set_auth_provider(app, _FakeAuthProvider.get_or_create("alice"))  # pyright: ignore[reportArgumentType]

    with pytest.raises(AttributeError):
        _auth.set_auth_provider(app, _FakeAuthProvider.get_or_create("bob"))  # pyright: ignore[reportArgumentType]


def test_get_current_user_resolves_and_creates_a_user(
    store: SQLLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_auth, "get_auth_provider", lambda: _FakeAuthProvider("alice"))

    user = _auth.get_current_user(store)

    assert user.username == "alice"
    assert store.get_or_create_user("alice").id == user.id


def test_get_current_user_falls_back_to_anonymous_when_identity_is_none(
    store: SQLLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_auth, "get_auth_provider", lambda: _FakeAuthProvider(None))

    assert _auth.get_current_user(store).username == "anonymous"
