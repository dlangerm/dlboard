"""
Retention policies: a project, or one of its experiments, keeps its newest N runs or the runs of its last N days.

The plugin owns every opinion about this: the rule kinds, the grace period a change waits through, the slack runs may
pass a rule by, the server's caps (`DLBOARD_RETENTION_MAX_*`), and how a project stands against its rules. The core knows
only a run can be kept (`keep_runs`, which retention never deletes), a project's plugin-owned scoped settings
(`register_setting`), a section on the project page (`register_project_section`) and background tasks (`register_task`).

Rules and status are two scoped settings (`RetentionKey`); two tasks (`_tasks.py`) schedule a check of each project with a
rule, and do the checks in the custodian process; the project page's section (`_section.py`) shows and changes them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum

from dlboard.plugins.retention._rules import RetentionKey, RetentionSetting
from dlboard.plugins.retention._section import (
    register_retention_callbacks,
    render_retention_section,
    retention_summary,
)
from dlboard.plugins.retention._settings import RetentionSettings
from dlboard.plugins.retention._tasks import RetentionTask, run_checks, schedule_checks
from dlboard.serve import (
    ProjectSection,
    ProjectSlot,
    SettingRule,
    SettingWrite,
    Task,
    TaskMode,
    register_project_section,
    register_setting,
    register_task,
)

if TYPE_CHECKING:
    from typing import Any

    from dash import Dash


def _within_caps(write: SettingWrite) -> dict[str, Any]:
    """
    The rule being saved, if the writer may set it: an owner changes only what is pending, within the caps.

    The rule in effect and the grace period are not a writer's to pick: a write that changes `active` from what the scope
    holds (or has in effect by now) is refused to anyone but an admin. A pending rule past a cap is an admin's, and is marked
    so (`cap_exempt`, worked out here, never taken from the writer) for the scheduling task never to lower it.
    """
    setting = RetentionSetting.model_validate(write.value)
    stored = RetentionSetting.model_validate(write.stored) if write.stored else RetentionSetting()
    if not write.admin and setting.active not in (
        stored.active,
        stored.as_of(pendulum.now(pendulum.UTC)).active,
    ):
        msg = "The rule in effect is the server's to change: save a rule, and it takes effect after its grace period."
        raise PermissionError(msg)
    pending = setting.pending
    if pending is None:
        return write.value
    past_cap = not RetentionSettings().within_caps(pending.rule)
    if past_cap and not write.admin:
        msg = "A rule that keeps more than this server's cap (DLBOARD_RETENTION_MAX_*) needs an admin."
        raise PermissionError(msg)
    return RetentionSetting(
        active=setting.active, pending=pending.model_copy(update={"cap_exempt": past_cap})
    ).dump()


def _only_the_system(_write: SettingWrite) -> dict[str, Any]:
    """No request writes a project's status: only the task that checked it does."""
    msg = "A project's retention status is recorded by the server."
    raise PermissionError(msg)


def plug(app: Dash) -> None:
    """Register retention's settings, its two tasks, and its sections on the project page."""
    register_setting(app, SettingRule(key=RetentionKey.RULE, check=_within_caps))
    register_setting(app, SettingRule(key=RetentionKey.STATUS, check=_only_the_system))
    every = pendulum.duration(seconds=RetentionSettings().retention_interval_s)
    register_task(app, Task(name=RetentionTask.SCHEDULE, run_once=schedule_checks, every=every))
    register_task(app, Task(name=RetentionTask.CHECK, run_once=run_checks, every=every, mode=TaskMode.SHARED))
    register_project_section(app, ProjectSection(ProjectSlot.HEADER, retention_summary))
    register_project_section(app, ProjectSection(ProjectSlot.BODY, render_retention_section))
    register_retention_callbacks(app)
