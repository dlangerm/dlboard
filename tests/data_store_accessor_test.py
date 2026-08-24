# pyright: reportPrivateUsage=false
"""Tests for `dltrack.serve._backend._data_store`: the data-store accessor and its scope-enforcing wrap."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dltrack.serve._backend import _data_store
from dltrack.serve._backend._scope_enforcement import ScopeEnforcingDataStore

if TYPE_CHECKING:
    from collections.abc import Iterator


class _FakeStore:
    """A bare `DataStore` stand-in -- `set_data_store` only needs something to wrap."""


class _FakeApp:
    """A bare stand-in for `Dash` -- `set_data_store`/`get_data_store` only need `hasattr`/`setattr`."""


@pytest.fixture(autouse=True)
def _clear_data_store_cache() -> Iterator[None]:
    """`get_data_store` is `functools.cache`d process-wide -- clear it before and after so a test
    that points it at a fake app (via `get_app`) doesn't leak that into, or get polluted by, any
    other test in the suite."""
    _data_store.get_data_store.cache_clear()
    yield
    _data_store.get_data_store.cache_clear()


def test_set_data_store_wraps_whatever_it_is_given_in_the_scope_enforcer() -> None:
    app = _FakeApp()
    inner = _FakeStore()

    _data_store.set_data_store(app, inner)  # pyright: ignore[reportArgumentType]

    stored = getattr(app, _data_store._DLTRACK_STORE_ATTRIBUTE)
    assert isinstance(stored, ScopeEnforcingDataStore)
    assert stored._inner is inner


def test_get_data_store_returns_the_scope_enforcing_wrapper_not_the_raw_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The round trip through the public `set_data_store`/`get_data_store` pair must never hand
    back a bare, unwrapped `DataStore` -- that would be the exact bypass `ScopeEnforcingDataStore`
    exists to close."""
    app = _FakeApp()
    inner = _FakeStore()
    _data_store.set_data_store(app, inner)  # pyright: ignore[reportArgumentType]
    monkeypatch.setattr(_data_store, "get_app", lambda: app)

    result = _data_store.get_data_store()

    assert isinstance(result, ScopeEnforcingDataStore)
    assert result is not inner
    assert result._inner is inner
