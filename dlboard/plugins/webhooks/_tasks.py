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

Either drops a run's end that ended more than `DLBOARD_WEBHOOK_EXPIRE_H` ago, unposted: what the server was down for, or
what was queued before the plugin was taken off the deployment's list and has waited since for it to be listed again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel
from structlog.stdlib import get_logger

from dlboard import models
from dlboard._compat import StrEnum
from dlboard._telemetry import Attributes, attribute
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
from dlboard.serve.telemetry import Counter, Unit

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


def _deliveries(store: DataStore[...], ended: RunEnded, settings: WebhookSettings) -> list[Delivery]:
    """What one end of a run is sent: a delivery for each webhook of its project that asks for how it ended."""
    if settings.expired(ended.done_at):
        return []
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
    store, tasks, settings = get_system_data_store(ctx.app), get_system_task_store(ctx.app), WebhookSettings()
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
                    due += _deliveries(store, RunEnded.model_validate(item.payload), settings)
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


class DeliveryOutcome(StrEnum):
    """What became of one delivery this pass: the label of `dlboard.webhook.deliveries`."""

    DELIVERED = "delivered"
    UNWANTED = "unwanted"
    """Its webhook was deleted or turned off since it was queued: dropped, not posted."""
    RETRY = "retry"
    """It didn't land (or its webhook already failed this pass): tried again later."""
    REJECTED = "rejected"
    """The receiver refused it outright (a 4xx): dropped, the webhook kept on."""
    TURNED_OFF = "turned_off"
    """It failed for the last time, or the receiver said it is gone: its webhook is turned off."""
    EXPIRED = "expired"
    """Its run ended longer ago than `DLBOARD_WEBHOOK_EXPIRE_H`: dropped, never posted that late."""


class _WebhookLabel(StrEnum):
    OUTCOME = "dlboard.webhook.outcome"


@dataclass(frozen=True, slots=True)
class _DeliveryAttributes(Attributes):
    outcome: DeliveryOutcome = attribute(_WebhookLabel.OUTCOME)


_SETTLED = Counter[_DeliveryAttributes](
    "dlboard.webhook.deliveries", Unit.ITEMS, "Webhook deliveries settled, by what became of them."
)


def _post(store: DataStore[...], delivery: Delivery) -> RefusedError | DeliveryOutcome:
    """Post one delivery, if its webhook is still there and on: whether it landed (or wasn't wanted), or why it didn't."""
    found = store.get_scoped_settings(WebhookKey.HOOKS, delivery.body.project.id).project
    hook = ProjectWebhooks.model_validate(found.value).find(delivery.hook_id) if found is not None else None
    if hook is None or hook.turned_off is not None:
        return DeliveryOutcome.UNWANTED
    try:
        send(hook, delivery.body, delivery_id(delivery.key))
    except RefusedError as refused:
        return refused
    return DeliveryOutcome.DELIVERED


@dataclass
class _DeliveryPass:
    """One run of the delivery task: what it posts with, and what it learns on the way."""

    ctx: TaskContext
    store: DataStore[...]
    settings: WebhookSettings = field(default_factory=WebhookSettings)
    down: dict[str, RefusedError] = field(default_factory=dict)
    """The webhooks a post failed to this run, and why: the rest of theirs wait for the next try, not posted again now."""
    expired: int = 0
    """Deliveries dropped unposted, their run having ended more than `DLBOARD_WEBHOOK_EXPIRE_H` ago."""

    def settle(self, item: WorkItem) -> None:
        """Settle one delivery's item (see `_settled`), and count what became of it."""
        _SETTLED.add(1, _DeliveryAttributes(outcome=self._settled(item)))

    def _settled(self, item: WorkItem) -> DeliveryOutcome:
        """Post one delivery, unless it expired or its webhook failed already this run; settle its item by how it went."""
        ctx, store, delivery = self.ctx, self.store, Delivery.model_validate(item.payload)
        if self.settings.expired(delivery.body.run.done_at):
            self.expired += 1
            ctx.done([item])
            return DeliveryOutcome.EXPIRED
        try:
            posted = self.down.get(delivery.hook_id) or _post(store, delivery)
        except Exception as exc:  # noqa: BLE001 -- one delivery's failure must not stop the others
            _log.warning("A webhook delivery failed", exc_info=True)
            ctx.retry(item, f"{type(exc).__name__}: {exc}")
            return DeliveryOutcome.RETRY
        match posted:
            case DeliveryOutcome():
                ctx.done([item])
                return posted
            case RefusedError():
                return self._refused(item, delivery, posted)

    def _refused(self, item: WorkItem, delivery: Delivery, refused: RefusedError) -> DeliveryOutcome:
        """Settle a delivery that didn't land, by why: dropped, tried again later, or its webhook turned off."""
        ctx, store = self.ctx, self.store
        match refused.failure:
            case Failure.REJECTED:
                _log.warning(
                    "A webhook refused a delivery; dropped it", hook_id=delivery.hook_id, why=str(refused)
                )
                ctx.done([item])
                return DeliveryOutcome.REJECTED
            case Failure.GONE:
                _turn_off(store, delivery.body.project.id, delivery.hook_id, f"Turned off: {refused}")
                ctx.done([item])
                return DeliveryOutcome.TURNED_OFF
            case Failure.RETRY if item.attempts >= self.settings.attempts:
                why = f"Turned off after {item.attempts} failed deliveries: {refused}"
                _turn_off(store, delivery.body.project.id, delivery.hook_id, why)
                ctx.done([item])
                return DeliveryOutcome.TURNED_OFF
            case Failure.RETRY:
                self.down[delivery.hook_id] = refused
                ctx.retry(item, str(refused))
                return DeliveryOutcome.RETRY


def deliver(ctx: TaskContext) -> None:
    """Post the deliveries that are due, a batch at a time."""
    this_pass = _DeliveryPass(ctx, get_system_data_store(ctx.app))
    while not ctx.should_stop():
        with ctx.batch():
            claimed = ctx.claim(WebhookWork.DELIVERY, limit=_DELIVERIES)
            for item in claimed:
                this_pass.settle(item)
        if not claimed:
            break
    if this_pass.expired:
        _log.warning(
            "Dropped webhook deliveries of runs that ended too long ago to post",
            dropped=this_pass.expired,
            expire_h=this_pass.settings.expire_h,
        )
    ctx.report(backlog=get_system_task_store(ctx.app).count_work(WebhookWork.DELIVERY))
