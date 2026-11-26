"""
Webhooks' two background tasks: one finds the runs that ended, the other delivers what each webhook is sent.

- **Finding** (one at a time, every `DLBOARD_WEBHOOK_INTERVAL_S`) reads the runs that ended since it last looked
  (`runs_done_since`, from a cursor it keeps in its own progress) a page at a time, and queues a delivery for each webhook
  of the run's project that asks for how it ended. A server with no webhooks reads one setting and nothing else.
- **Delivering** (any custodian, a few deliveries at a time) posts each one. One that fails is tried again later (a
  minute, doubling); one that has failed `DLBOARD_WEBHOOK_ATTEMPTS` times turns its webhook off, and the project's
  Webhooks tab says why. Each post has short timeouts, so a slow or dead endpoint holds up only its own delivery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pendulum
from structlog.stdlib import get_logger

from dlboard import models
from dlboard._compat import StrEnum
from dlboard.plugins.webhooks._hooks import (
    ProjectWebhooks,
    RefusedError,
    WebhookEvent,
    WebhookKey,
    WebhookSettings,
    body_for,
    send,
    subscribed,
)
from dlboard.serve import get_system_data_store
from dlboard.serve._custodian import NewWorkItem, get_system_task_store

if TYPE_CHECKING:
    from datetime import datetime

    from dlboard.models import DataStore
    from dlboard.serve import TaskContext

_log = get_logger(__name__)

_PAGE = 200
"""Runs read a batch when finding what ended."""
_DELIVERIES = 10
"""Deliveries claimed a batch."""

WEBHOOKS_PRINCIPAL = models.Principal.system("webhooks")
"""Who turns a failing webhook off."""


class WebhookTask(StrEnum):
    """The plugin's tasks."""

    FIND = "webhook_find"
    DELIVER = "webhook_deliver"


class WebhookWork(StrEnum):
    """The plugin's work items. Stored as their kind, so a value is never renamed or removed."""

    DELIVERY = "webhook_delivery"
    """One webhook to post one run's end to: `<hook>:<run>:<when it ended>` is its key, so a run ending once is sent once."""


def _cursor(ctx: TaskContext) -> tuple[datetime, int] | None:
    """Where finding left off, from the progress it last reported."""
    state = next(
        (s for s in get_system_task_store(ctx.app).task_states() if s.name == WebhookTask.FIND), None
    )
    found: list[Any] | None = state.progress.get("cursor") if state is not None else None
    if not found:
        return None
    when = pendulum.parse(str(found[0]))
    return (when, int(found[1])) if isinstance(when, pendulum.DateTime) else None


def find_ended_runs(ctx: TaskContext) -> None:
    """Queue a delivery for each webhook that asks for how each run that ended since the last look ended."""
    store, tasks = get_system_data_store(ctx.app), get_system_task_store(ctx.app)
    hooks = {
        project_id: ProjectWebhooks.model_validate(found.project.value).hooks
        for project_id, found in store.all_scoped_settings(WebhookKey.HOOKS).items()
        if found.project is not None
    }
    made = [hook.created_at for project in hooks.values() for hook in project]
    if not made:
        ctx.report(backlog=0, cursor=None)  # nothing to announce to, nor a place to start from
        return
    # A first look starts where the first webhook was made: what ended before is no one's news. Not "now", which a clock
    # stepped back could put after a run that ended just as it was taken.
    cursor = _cursor(ctx) or (min(made), 0)
    queued = 0
    while not ctx.should_stop():
        with ctx.batch():
            runs = store.runs_done_since(cursor, limit=_PAGE)
            projects = {
                e.id: e.project_id for e in store.get_experiments_by_id({r.experiment_id for r in runs})
            }
            due = [
                NewWorkItem(
                    kind=WebhookWork.DELIVERY,
                    key=f"{hook.id}:{run.id}:{run.done_at.isoformat() if run.done_at else ''}",
                    payload={"project_id": projects[run.experiment_id], "hook_id": hook.id, "run_id": run.id},
                )
                for run in runs
                if (event := WebhookEvent.of(run)) is not None and run.experiment_id in projects
                for hook in subscribed(hooks.get(projects[run.experiment_id], ()), event, run)
            ]
            tasks.enqueue_work(due)
        queued += len(due)
        if runs:
            last = runs[-1]
            cursor = (last.done_at or cursor[0], last.id)
        if len(runs) < _PAGE:
            break
    ctx.report(backlog=tasks.count_work(WebhookWork.DELIVERY), cursor=[cursor[0].isoformat(), cursor[1]])
    if queued:
        tasks.request_task_run(WebhookTask.DELIVER)


def _turn_off(store: DataStore[...], project_id: int, hook_id: str, why: str) -> None:
    found = store.get_scoped_settings(WebhookKey.HOOKS, project_id).project
    if found is None:
        return
    hooks = ProjectWebhooks.model_validate(found.value)
    hook = hooks.find(hook_id)
    if hook is None or hook.turned_off is not None:
        return
    changed = hooks.replaced(hook_id, hook.model_copy(update={"turned_off": why}))
    actor = store.get_or_create_user(WEBHOOKS_PRINCIPAL)
    store.put_scoped_setting(WebhookKey.HOOKS, project_id, None, changed.model_dump(mode="json"), actor)
    _log.warning("Turned a webhook off: its deliveries kept failing", project_id=project_id, hook_id=hook_id)


def _deliver(store: DataStore[...], payload: dict[str, Any]) -> None:
    """Post one delivery; nothing, if its webhook or run is gone or the webhook is off. `RefusedError` if it failed."""
    project_id, hook_id, run_id = int(payload["project_id"]), str(payload["hook_id"]), int(payload["run_id"])
    found = store.get_scoped_settings(WebhookKey.HOOKS, project_id).project
    hook = ProjectWebhooks.model_validate(found.value).find(hook_id) if found is not None else None
    run = store.get_run(run_id)
    if hook is None or hook.turned_off is not None or run is None or (event := WebhookEvent.of(run)) is None:
        return
    experiment, project = store.get_experiment(run.experiment_id), store.get_project(project_id)
    if experiment is None or project is None:
        return
    send(hook, event, body_for(event, run, experiment, project, hook.link_base))


def deliver(ctx: TaskContext) -> None:
    store, attempts = get_system_data_store(ctx.app), WebhookSettings().attempts
    while not ctx.should_stop():
        claimed = ctx.claim(WebhookWork.DELIVERY, limit=_DELIVERIES)
        if not claimed:
            break
        for item in claimed:
            try:
                _deliver(store, item.payload)
            except RefusedError as refused:
                if item.attempts >= attempts:
                    why = f"Turned off after {item.attempts} failed deliveries: {refused}"
                    _turn_off(store, int(item.payload["project_id"]), str(item.payload["hook_id"]), why)
                    ctx.done([item])
                else:
                    _log.info("A webhook delivery failed; trying it again later", error=str(refused))
                    ctx.retry(item, str(refused))
            except Exception as exc:  # noqa: BLE001 -- one delivery's failure must not stop the others
                _log.warning("A webhook delivery failed", exc_info=True)
                ctx.retry(item, f"{type(exc).__name__}: {exc}")
            else:
                ctx.done([item])
    ctx.report(backlog=get_system_task_store(ctx.app).count_work(WebhookWork.DELIVERY))
