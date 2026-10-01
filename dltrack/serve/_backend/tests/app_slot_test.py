# pyright: reportPrivateUsage=false
"""Tests for `AppSlot` (per-app plugin state) and the per-request data store accessor."""

from __future__ import annotations

import threading

import pytest

from dltrack.serve._backend import _app_slot, _data_store
from dltrack.serve._backend._app_slot import AppSlot


class _FakeApp:
    """A bare stand-in for `Dash` -- an `AppSlot` only needs `hasattr`/`setattr` on it."""


def test_set_refuses_to_overwrite() -> None:
    slot = AppSlot[str]("thing")
    app = _FakeApp()
    slot.set(app, "a")  # pyright: ignore[reportArgumentType]

    with pytest.raises(AttributeError, match="Refusing to overwrite"):
        slot.set(app, "b")  # pyright: ignore[reportArgumentType]


def test_get_follows_the_current_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accessors used to be `functools.cache`d, pinning every later app in a process to the first one's state."""
    slot = AppSlot[str]("thing")
    first, second = _FakeApp(), _FakeApp()
    slot.set(first, "first")  # pyright: ignore[reportArgumentType]
    slot.set(second, "second")  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(_app_slot, "get_app", lambda: first)
    from_first = slot.get()
    monkeypatch.setattr(_app_slot, "get_app", lambda: second)

    assert (from_first, slot.get()) == ("first", "second")


def test_wait_blocks_until_set_on_that_specific_app() -> None:
    slot = AppSlot[str]("thing")
    app = _FakeApp()
    result: list[str] = []
    waiter = threading.Thread(target=lambda: result.append(slot.wait(app)))  # pyright: ignore[reportArgumentType]
    waiter.start()

    slot.set(_FakeApp(), "some other app's")  # pyright: ignore[reportArgumentType]
    slot.set(app, "mine")  # pyright: ignore[reportArgumentType]
    waiter.join(timeout=5)

    assert result == ["mine"]


def test_get_data_store_refuses_outside_a_request() -> None:
    """Outside a request there's nobody to authorize as -- background work must say so explicitly."""
    with pytest.raises(RuntimeError, match="get_system_data_store"):
        _data_store.get_data_store()
