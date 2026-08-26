# pyright: reportPrivateUsage=false
"""
Tests for `dltrack.serve.set_setting_env`.

Callers (the CLI, `run_production_server`) set env vars for `pydantic_settings.BaseSettings`
classes they don't own the definition of. These tests exist so a rename of a settings field breaks
loudly here instead of silently setting an env var nothing reads anymore.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dltrack.plugins.data_stores.filesystem import AppSettings as FilesystemAppSettings
from dltrack.plugins.data_stores.sqlite import AppSettings as SqliteAppSettings
from dltrack.serve import set_setting_env


def test_set_setting_env_is_picked_up_by_the_real_sqlite_app_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SQLITE_LOCATION", raising=False)
    location = Path("/tmp/some-test.sqlite")

    set_setting_env(SqliteAppSettings, "sqlite_location", location)

    assert SqliteAppSettings().sqlite_location == location


def test_set_setting_env_is_picked_up_by_the_real_filesystem_app_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARTIFACT_STORE_LOCATION", raising=False)
    location = Path("/tmp/some-test-artifacts")

    set_setting_env(FilesystemAppSettings, "artifact_store_location", location)

    assert FilesystemAppSettings().artifact_store_location == location


def test_set_setting_env_rejects_a_field_that_does_not_exist() -> None:
    with pytest.raises(AttributeError, match="no field"):
        set_setting_env(SqliteAppSettings, "not_a_real_field", "whatever")
