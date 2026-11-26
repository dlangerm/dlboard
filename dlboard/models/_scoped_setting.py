"""
A plugin's own settings for a project, or one of its experiments: JSON under a key the plugin owns.

The core stores and authorizes them (`SettingRule`, registered by the plugin, says who may read and write each scope) and
knows nothing of what is in them, so a plugin keeps whatever shape of setting it needs without a table of its own.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Any

import pendulum
from pydantic import AwareDatetime, BaseModel, Field


class Overwrite(Enum):
    """`put_scoped_setting`'s `expected` when a write replaces whatever the scope holds, unread: the last write wins."""

    ANYTHING = auto()


class SettingConflictError(ValueError):
    """A write that expected the scope to hold what it read, made after someone else changed it: read it again."""


class NewScopedSetting(BaseModel, frozen=True, extra="forbid"):
    """One plugin setting of a project (`experiment_id` of `None`) or of one of its experiments."""

    key: str
    """The plugin's own name for the setting, e.g. `retention.rule`."""
    project_id: int
    experiment_id: int | None = None
    value: dict[str, Any] = {}
    """The plugin's own JSON: the core never looks inside it."""
    updated_by: int | None = None
    updated_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))


class ScopedSetting(NewScopedSetting, frozen=True, extra="forbid"):
    """A stored plugin setting."""

    id: int


class ScopedSettings(BaseModel, frozen=True, extra="forbid"):
    """A project's setting under one key, and each of its (non-deleted) experiments' that has one."""

    project: ScopedSetting | None = None
    experiments: dict[int, ScopedSetting] = {}
