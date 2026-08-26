# pyright: reportPrivateUsage=false
"""Tests for `dltrack.serve._production_server`: resolving a plugin list from an import path."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dltrack.serve import resolve_plugins
from dltrack.serve._production_server import WSGISettings


def test_resolve_plugins_imports_a_real_plugin_list() -> None:
    plugins = resolve_plugins("dltrack.plugins:LOCAL_DEPLOYMENT")

    assert len(plugins) > 0
    assert all(callable(getattr(plugin, "plug", None)) for plugin in plugins)


def test_resolve_plugins_rejects_an_unimportable_module() -> None:
    with pytest.raises(ValidationError, match="import_error"):
        resolve_plugins("not.a.real.module:PLUGINS")


def test_resolve_plugins_rejects_a_list_of_non_plugins() -> None:
    with pytest.raises(ValidationError, match="instance of PluginProtocol"):
        resolve_plugins("dltrack.models:__all__")


def test_wsgi_settings_reads_from_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLTRACK_PLUGINS", "dltrack.plugins:LOCAL_DEPLOYMENT")

    assert len(WSGISettings().dltrack_plugins) > 0  # pyright: ignore[reportCallIssue]
