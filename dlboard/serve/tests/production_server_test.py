"""Tests for `dlboard.serve._production_server`: resolving a plugin list from an import path, and the TLS settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from dlboard.serve import resolve_plugins
from dlboard.serve._production_server import TLSSettings, WSGISettings

if TYPE_CHECKING:
    from pathlib import Path


def test_resolve_plugins_imports_a_real_plugin_list() -> None:
    plugins = resolve_plugins("dlboard.plugins:LOCAL_DEPLOYMENT")

    assert len(plugins) > 0
    assert all(callable(getattr(plugin, "plug", None)) for plugin in plugins)


def test_resolve_plugins_rejects_an_unimportable_module() -> None:
    with pytest.raises(ValidationError, match="import_error"):
        resolve_plugins("not.a.real.module:PLUGINS")


def test_resolve_plugins_rejects_a_list_of_non_plugins() -> None:
    with pytest.raises(ValidationError, match="instance of PluginProtocol"):
        resolve_plugins("dlboard.models:__all__")


def test_wsgi_settings_reads_from_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLBOARD_PLUGINS", "dlboard.plugins:LOCAL_DEPLOYMENT")

    assert len(WSGISettings().dlboard_plugins) > 0


@pytest.mark.parametrize(
    ("given", "missing"), [("CERTIFICATE", "DLBOARD_SSL_KEYFILE"), ("KEYFILE", "DLBOARD_SSL_CERTIFICATE")]
)
def test_half_a_certificate_and_key_is_an_error_naming_what_is_missing(
    given: str, missing: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Never a silent fall back to plain HTTP (#797)."""
    pem = tmp_path / "file.pem"
    pem.write_text("")
    monkeypatch.setenv(f"DLBOARD_SSL_{given}", str(pem))
    with pytest.raises(ValidationError, match=f"set {missing} too"):
        TLSSettings()


def test_no_certificate_and_no_key_is_plain_http() -> None:
    assert not TLSSettings().enabled
