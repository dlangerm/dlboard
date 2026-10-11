"""
Tests for `dlboard.serve.set_setting_env`.

Callers (the CLI, `run_production_server`) set env vars for `pydantic_settings.BaseSettings`
classes they don't own the definition of. These tests exist so a rename of a settings field breaks
loudly here instead of silently setting an env var nothing reads anymore.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from dlboard.plugins.data_stores.filesystem import AppSettings as FilesystemAppSettings
from dlboard.plugins.data_stores.sqlite import AppSettings as SqliteAppSettings
from dlboard.serve import set_setting_env

if TYPE_CHECKING:
    from pydantic_settings import BaseSettings


@pytest.mark.parametrize(
    ("settings_cls", "field", "env_var", "location"),
    [
        pytest.param(
            SqliteAppSettings,
            "sqlite_location",
            "DLBOARD_SQLITE_LOCATION",
            Path("/tmp/some-test.sqlite"),
            id="sqlite",
        ),
        pytest.param(
            FilesystemAppSettings,
            "artifact_store_location",
            "DLBOARD_ARTIFACT_STORE_LOCATION",
            Path("/tmp/some-test-artifacts"),
            id="filesystem",
        ),
    ],
)
def test_set_setting_env_is_picked_up_by_the_real_app_settings(
    monkeypatch: pytest.MonkeyPatch,
    settings_cls: type[BaseSettings],
    field: str,
    env_var: str,
    location: Path,
) -> None:
    monkeypatch.delenv(env_var, raising=False)

    set_setting_env(settings_cls, field, location)

    assert getattr(settings_cls(), field) == location


def test_set_setting_env_rejects_a_field_that_does_not_exist() -> None:
    with pytest.raises(AttributeError, match="no field"):
        set_setting_env(SqliteAppSettings, "not_a_real_field", "whatever")
