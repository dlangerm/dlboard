"""
Who may read and write a plugin's scoped settings (`DataStore.put_scoped_setting`): the rule the plugin registers.

The core authorizes every read and write of a setting through its key's rule (`AuthorizingDataStore`), so a plugin never
authorizes by hand: it says which role each scope needs, and may check a value against what only it knows (a server's
cap, say) before it is stored. A key no plugin registered a rule for is read and written by the system alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from dlboard.models import ProjectRole
from dlboard.serve._backend._app_slot import AppSlot

if TYPE_CHECKING:
    from collections.abc import Callable

    from dash import Dash

    from dlboard._compat import StrEnum


@dataclass(frozen=True)
class SettingWrite:
    """A write a rule's `check` is asked about: the scope, the value, what it replaces, and whether the writer is an admin."""

    project_id: int
    experiment_id: int | None
    value: dict[str, Any]
    stored: dict[str, Any] | None
    """What the scope holds now (`None`: no setting), so a check can tell a change from a value that was not touched."""
    admin: bool
    """Whether the writer has `Scope.ALL`: an admin may set what an owner may not."""


@dataclass(frozen=True)
class SettingRule:
    """The roles a plugin's setting `key` needs, and the check its values pass before they are stored."""

    key: StrEnum
    read: ProjectRole = ProjectRole.VIEWER
    write_project: ProjectRole = ProjectRole.OWNER
    """The role a project's own setting needs: `OWNER` is an owner, or an admin wherever grants are not enforced."""
    write_experiment: ProjectRole = ProjectRole.EDITOR
    check: Callable[[SettingWrite], dict[str, Any]] | None = None
    """
    Asked about every write a request makes: the value to store (as given, or marked up), or `PermissionError` if the
    writer may not set it.
    """


SETTING_RULES: AppSlot[dict[str, SettingRule]] = AppSlot("setting rules")


def register_setting(app: Dash, rule: SettingRule) -> None:
    """Let requests read and write the setting `rule.key`, as `rule` says."""
    rules = SETTING_RULES.find(app)
    if rules is None:
        rules = dict[str, SettingRule]()
        SETTING_RULES.set(app, rules)
    if str(rule.key) in rules:
        msg = f"A rule for the setting {rule.key!r} is already registered"
        raise ValueError(msg)
    rules[str(rule.key)] = rule
