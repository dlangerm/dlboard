"""Coupling a resolved value to the env var a `pydantic_settings.BaseSettings` field reads."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic_settings import BaseSettings


def set_setting_env(settings_cls: type[BaseSettings], field_name: str, value: object) -> None:
    """
    Set the environment variable that `settings_cls`'s pydantic-settings field `field_name` reads.

    Keeps a caller's env var names coupled to the actual settings fields, rather than hardcoding
    env var name strings that could silently drift from what the field was renamed to: this raises
    immediately if `field_name` doesn't exist on `settings_cls`, instead of setting an env var
    nothing reads anymore. Relies on `settings_cls` using pydantic-settings' default per-field alias
    env var derivation, `env_prefix + field_name.upper()` -- not a field-level `alias`/`validation_alias`.
    """
    if field_name not in settings_cls.model_fields:
        msg = f"{settings_cls.__qualname__} has no field {field_name!r}"
        raise AttributeError(msg)
    prefix = settings_cls.model_config.get("env_prefix", "")
    os.environ[f"{prefix}{field_name}".upper()] = str(value)
