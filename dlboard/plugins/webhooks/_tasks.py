"""
Webhooks' two background tasks: one finds what each run's end is sent, the other delivers it.

- **Finding** (any custodian, a page at a time) claims the runs' ends the store queued as each was recorded
  (`register_run_end_work`), and queues a delivery, its body made then, for each webhook of the run's project that asks
  for how it ended. An end is queued in the transaction that records it, so every end that commits is found, once, with
  no cursor and no wait for slower transactions. One whose run was deleted, or heard from again, by then is dropped.
- **Delivering** (any custodian, a few deliveries at a time) posts each one. One that fails is tried again later (the
  custodian's `Backoff`); one that has failed `DLBOARD_WEBHOOK_ATTEMPTS` times, or that the receiver says is gone, turns its
  webhook off, and the project's Webhooks tab says why. A post the receiver refuses outright is dropped. Each post has
  short timeouts, and a webhook that fails once is not tried again in the same pass, so a dead endpoint holds up only
  its own deliveries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel
from structlog.stdlib import get_logger

from dlboard import models
from dlboard._compat import StrEnum
from dlboard.plugins.webhooks._hooks import (
    Failure,
    ProjectWebhooks,
    RefusedError,
    WebhookEvent,
    WebhookKey,
    WebhookPayload,
    WebhookSettings,
    delivery_id,
    send,
    subscribed,
)
from dlboard.serve import RunEnded, get_system_data_store
from dlboard.serve._custodian import NewWorkItem, get_system_task_store

if TYPE_CHECKING:
    from dlboard.models import DataStore
    from dlboard.serve import TaskContext, WorkItem

_log = get_logger(__name__)

_ENDS = 100
"""Run ends claimed a batch."""
_DELIVERIES = 10
"""Deliveries claimed a batch."""
_CONFLICT_TRIES = 3
"""Times turning a webhook off reads its project's webhooks again, when its owner changed them in between."""

WEBHOOKS_PRINCIPAL = models.Principal.system("webhooks")
"""Who turns a failing webhook off."""


class WebhookTask(StrEnum):
    """The plugin's tasks."""

    FIND = "webhook_find"
    DELIVER = "webhook_deliver"


class WebhookWork(StrEnum):
    """The plugin's work items. Stored as their kind, so a value is never renamed or removed."""

    RUN_END = "webhook_run_end"
    """A run's end, queued by the store as it is recorded (`RunEnded`)."""
    DELIVERY = "webhook_delivery"


class Delivery(BaseModel, frozen=True, extra="forbid"):
    """One webhook to post one run's end to, with the body it is posted, made as the end was found."""

    hook_id: str
    body: WebhookPayload

    @property
    def key(self) -> str:
        """Unique to one end of one run for one webhook: a run ending once is queued once."""
        return f"{self.hook_id}:{self.body.run.id}:{self.body.run.done_at.isoformat()}"

    def queued(self) -> NewWorkItem:
        return NewWorkItem(kind=WebhookWork.DELIVERY, key=self.key, payload=self.model_dump(mode="json"))


def _deliveries(store: DataStore[...], ended: RunEnded) -> list[Delivery]:
    """What one end of a run is sent: a delivery for each webhook of its project that asks for how it ended."""
    run = store.get_run(ended.run_id)
    if (
        run is None or run.done_at != ended.done_at
    ):  # deleted, or running again (and maybe ended another way) since
        return []
    event = WebhookEvent.of(run)
    experiment = store.get_experiment(run.experiment_id)
    project = store.get_project(experiment.project_id) if experiment is not None else None
    if event is None or experiment is None or project is None:
        return []
    found = store.get_scoped_settings(WebhookKey.HOOKS, project.id).project
    hooks = ProjectWebhooks.model_validate(found.value).hooks if found is not None else ()
    return [
        Delivery(
            hook_id=hook.id,
            body=WebhookPayload.of(event, run, ended.done_at, experiment, project, hook.link_base),
        )
        for hook in subscribed(hooks, event, run)
    ]


def find_ended_runs(ctx: TaskContext) -> None:
    """Queue a delivery for each webhook that asks for how each run whose end was queued ended."""
    store, tasks = get_system_data_store(ctx.app), get_system_task_store(ctx.app)
    queued = 0
    while not ctx.should_stop():
        claimed = ctx.claim(WebhookWork.RUN_END, limit=_ENDS)
        if not claimed:
            break
        with ctx.batch():
            due: list[Delivery] = []
            found: list[WorkItem] = []
            for item in claimed:
                try:
                    due += _deliveries(store, RunEnded.model_validate(item.payload))
                except Exception as exc:  # noqa: BLE001 -- one end's failure must not hold up the others
                    _log.warning("Finding what a run's end is sent failed", exc_info=True)
                    ctx.retry(item, f"{type(exc).__name__}: {exc}")
                    continue
                found.append(item)
            tasks.enqueue_work(delivery.queued() for delivery in due)
            ctx.done(
                found
            )  # after the deliveries: an end claimed again, should this stop between, queues none twice
        queued += len(due)
    ctx.report(backlog=tasks.count_work(WebhookWork.DELIVERY))
    if queued:
        tasks.request_task_run(WebhookTask.DELIVER)


def _turn_off(store: DataStore[...], project_id: int, hook_id: str, why: str) -> None:
    """Turn the webhook off, saying why, without undoing a change its owner made to the project's webhooks meanwhile."""
    actor = store.get_or_create_user(WEBHOOKS_PRINCIPAL)
    for _ in range(_CONFLICT_TRIES):
        found = store.get_scoped_settings(WebhookKey.HOOKS, project_id).project
        hooks = ProjectWebhooks.model_validate(found.value) if found is not None else ProjectWebhooks()
        hook = hooks.find(hook_id)
        if hook is None or hook.turned_off is not None:
            return
        changed = hooks.replaced(hook_id, hook.model_copy(update={"turned_off": why}))
        try:
            store.put_scoped_setting(
                WebhookKey.HOOKS, project_id, None, changed.model_dump(mode="json"), actor, expected=found
            )
        except models.SettingConflictError:
            continue
        _log.warning("Turned a webhook off", project_id=project_id, hook_id=hook_id, why=why)
        return
    _log.warning("Couldn't turn a webhook off: its project's webhooks kept changing", hook_id=hook_id)


def _post(store: DataStore[...], delivery: Delivery) -> RefusedError | None:
    """Post one delivery, if its webhook is still there and on; why not, if it didn't land."""
    found = store.get_scoped_settings(WebhookKey.HOOKS, delivery.body.project.id).project
    hook = ProjectWebhooks.model_validate(found.value).find(delivery.hook_id) if found is not None else None
    if hook is None or hook.turned_off is not None:
        return None
    try:
        send(hook, delivery.body, delivery_id(delivery.key))
    except RefusedError as refused:
        return refused
    return None


def deliver(ctx: TaskContext) -> None:
    store, attempts = get_system_data_store(ctx.app), WebhookSettings().attempts
    down: dict[str, RefusedError] = {}
    """The webhooks a post failed to this run, and why: the rest of theirs wait for the next try, not posted again now."""
    while not ctx.should_stop():
        claimed = ctx.claim(WebhookWork.DELIVERY, limit=_DELIVERIES)
        if not claimed:
            break
        for item in claimed:
            delivery = Delivery.model_validate(item.payload)
            try:
                refused = down.get(delivery.hook_id) or _post(store, delivery)
            except Exception as exc:  # noqa: BLE001 -- one delivery's failure must not stop the others
                _log.warning("A webhook delivery failed", exc_info=True)
                ctx.retry(item, f"{type(exc).__name__}: {exc}")
                continue
            match refused:
                case None:
                    ctx.done([item])
                case RefusedError(failure=Failure.REJECTED):
                    _log.warning(
                        "A webhook refused a delivery; dropped it", hook_id=delivery.hook_id, why=str(refused)
                    )
                    ctx.done([item])
                case RefusedError(failure=Failure.GONE):
                    _turn_off(store, delivery.body.project.id, delivery.hook_id, f"Turned off: {refused}")
                    ctx.done([item])
                case RefusedError(failure=Failure.RETRY) if item.attempts >= attempts:
                    why = f"Turned off after {item.attempts} failed deliveries: {refused}"
                    _turn_off(store, delivery.body.project.id, delivery.hook_id, why)
                    ctx.done([item])
                case RefusedError(failure=Failure.RETRY):
                    down[delivery.hook_id] = refused
                    ctx.retry(item, str(refused))
    ctx.report(backlog=get_system_task_store(ctx.app).count_work(WebhookWork.DELIVERY))
