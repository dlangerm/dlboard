"""
Webhooks (#704): when a run ends, the server posts it, signed, to the URLs a project's owners set.

A run that finished, failed or crashed is announced, so a chat channel, a CI pipeline or a bot can take it from there. The
plugin owns every opinion about it: the events, the payload, the signature, which addresses the server may post to
(`DLBOARD_WEBHOOK_ALLOWED_HOSTS`), and when a webhook that keeps failing is turned off. The core knows only a project's
plugin-owned scoped settings (`register_setting`), a tab on the project page (`register_project_tab`), background tasks
(`register_task`), and each run's end, queued for it as the end is recorded (`register_run_end_work`): one row
in the transaction that ends the run is all a run's end costs. Delivery is work nobody waits on, done by the custodian.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum

from dlboard.models import ProjectRole
from dlboard.plugins.webhooks._hooks import WebhookKey, WebhookSettings
from dlboard.plugins.webhooks._tab import (
    WEBHOOKS_TAB,
    checked,
    register_webhook_callbacks,
    render_webhooks_tab,
)
from dlboard.plugins.webhooks._tasks import WebhookTask, WebhookWork, deliver, find_ended_runs
from dlboard.serve import (
    ProjectTab,
    SettingRule,
    Task,
    TaskMode,
    register_project_tab,
    register_run_end_work,
    register_setting,
    register_task,
)

if TYPE_CHECKING:
    from dash import Dash


def plug(app: Dash) -> None:
    """Register the webhooks setting (an owner's alone, secrets and all), the two tasks, and the project page's tab."""
    register_setting(app, SettingRule(key=WebhookKey.HOOKS, read=ProjectRole.OWNER, check=checked))
    every = pendulum.duration(seconds=WebhookSettings().interval_s)
    register_run_end_work(app, WebhookWork.RUN_END)
    register_task(
        app, Task(name=WebhookTask.FIND, run_once=find_ended_runs, every=every, mode=TaskMode.SHARED)
    )
    register_task(app, Task(name=WebhookTask.DELIVER, run_once=deliver, every=every, mode=TaskMode.SHARED))
    register_project_tab(app, ProjectTab(key=WEBHOOKS_TAB, label="Webhooks", render=render_webhooks_tab))
    register_webhook_callbacks(app)
