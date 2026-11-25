"""Settings helpers: coupling a resolved value to the env var a `BaseSettings` field reads, and comma-separated values."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Annotated

from pydantic import BeforeValidator
from pydantic_settings import NoDecode

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic_settings import BaseSettings


def set_setting_env(settings_cls: type[BaseSettings], field_name: str, value: str | Path) -> None:
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
    os.environ[f"{prefix}{field_name}".upper()] = str(value)  # dlboard-ignore[forbid-string-ignore]


def comma_items(text: str) -> list[str]:
    """`a, b,,c` as `["a", "b", "c"]`: each item stripped, the empty ones dropped."""
    return [item.strip() for item in text.split(",") if item.strip()]


def _split[T](value: str | T) -> list[str] | T:
    return comma_items(value) if isinstance(value, str) else value


type CommaSeparated[T] = Annotated[T, NoDecode, BeforeValidator(_split)]
"""
A settings field read from one comma-separated env var (`10.0.0.5, 172.18.0.0/16`), each item validated as `T`'s.

`NoDecode`, so pydantic-settings hands the raw string over instead of reading it as JSON; a value built in code, with the
items already apart, is left for validation to judge.
"""
