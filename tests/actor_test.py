"""Tests for `dltrack.plugins.pages._actor`: best-effort UI-driven actor resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dltrack.plugins.pages import _actor

if TYPE_CHECKING:
    import pytest

    from dltrack.plugins.data_stores.sqlite import SQLLiteStore


def test_current_actor_resolves_and_creates_a_user(
    store: SQLLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_actor, "resolve_username", lambda: "alice")

    actor = _actor.current_actor(store)

    assert actor.username == "alice"
    assert store.get_or_create_user("alice").id == actor.id


def test_current_actor_id_matches_current_actor(store: SQLLiteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_actor, "resolve_username", lambda: "bob")

    assert _actor.current_actor_id(store) == _actor.current_actor(store).id


def test_current_actor_reuses_the_same_user_on_repeat_calls(
    store: SQLLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_actor, "resolve_username", lambda: "carol")

    first = _actor.current_actor(store)
    second = _actor.current_actor(store)

    assert first == second
