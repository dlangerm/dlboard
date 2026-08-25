# pyright: reportPrivateUsage=false
"""Tests for `dltrack.serve._backend._installed_plugins`: the installed-plugin-snapshot accessor."""

from __future__ import annotations

import pytest

from dltrack.models import InstalledPlugin
from dltrack.serve._backend import _installed_plugins


class _FakeApp:
    """A bare stand-in for `Dash` -- `set_installed_plugins`/`get_installed_plugins` only need `hasattr`/`setattr`."""


def test_set_installed_plugins_refuses_to_overwrite_an_existing_list() -> None:
    app = _FakeApp()
    plugins = [InstalledPlugin(name="a")]
    _installed_plugins.set_installed_plugins(app, plugins)  # pyright: ignore[reportArgumentType]

    with pytest.raises(AttributeError):
        _installed_plugins.set_installed_plugins(app, plugins)  # pyright: ignore[reportArgumentType]


def test_get_installed_plugins_returns_exactly_what_was_set(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _FakeApp()
    plugins = [InstalledPlugin(name="a"), InstalledPlugin(name="b", description="does b things")]
    _installed_plugins.set_installed_plugins(app, plugins)  # pyright: ignore[reportArgumentType]
    monkeypatch.setattr(_installed_plugins, "get_app", lambda: app)
    _installed_plugins.get_installed_plugins.cache_clear()

    result = _installed_plugins.get_installed_plugins()

    assert result is plugins
    _installed_plugins.get_installed_plugins.cache_clear()
