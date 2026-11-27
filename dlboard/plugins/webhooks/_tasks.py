"""
Webhooks' two background tasks: one finds the runs that ended, the other delivers what each webhook is sent.

- **Finding** (one at a time, every `DLBOARD_WEBHOOK_INTERVAL_S`) reads the runs that ended since it last looked
  (`runs_done_since`, from a cursor it keeps in its own progress, up to `RUN_END_SETTLES` ago so that no end still being
  committed is passed over) a page at a time, and queues a delivery, its body made then, for each webhook of the run's
  project that asks for how it ended. A server with no webhooks reads one setting and nothing else.
- **Delivering** (any custodian, a few deliveries at a time) posts each one. One that fails is tried again later (a
  minute, doubling); one that has failed `DLBOARD_WEBHOOK_ATTEMPTS` times, or that the receiver says is gone, turns its
  webhook off, and the project's Webhooks tab says why. A post the receiver refuses outright is dropped. Each post has
  short timeouts, and a webhook that fails once is not tried again in the same pass, so a dead endpoint holds up only
  its own deliveries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum
from pydantic import AwareDatetime, BaseModel
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
from dlboard.serve import get_system_data_store
from dlboard.serve._custodian import NewWorkItem, get_system_task_store

if TYPE_CHECKING:
    from dlboard.models import DataStore
    from dlboard.plugins.webhooks._hooks import Webhook
    from dlboard.serve import TaskContext

_log = get_logger(__name__)

_PAGE = 200
"""Runs read a batch when finding what ended."""
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

    DELIVERY = "webhook_delivery"


class _Cursor(BaseModel, frozen=True, extra="forbid"):
    """Where finding left off: the last run it read, by when it ended."""

    done_at: AwareDatetime
    run_id: int


class _FindProgress(BaseModel, frozen=True, extra="forbid"):
    """What finding reports, and reads back on its next run: its backlog, and its cursor."""

    backlog: int | None = None
    cursor: _Cursor | None = None


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


def _progress(ctx: TaskContext) -> _FindProgress:
    """What finding last reported about itself."""
    states = get_system_task_store(ctx.app).task_states()
    state = next((s for s in states if s.name == WebhookTask.FIND), None)
    return _FindProgress.model_validate(state.progress) if state is not None else _FindProgress()


def _deliveries(
    store: DataStore[...], runs: list[models.Run], hooks: dict[int, tuple[Webhook, ...]]
) -> list[Delivery]:
    """What `runs` are sent: one delivery for each webhook of each run's project that asks for how it ended."""
    experiments = {e.id: e for e in store.get_experiments_by_id({r.experiment_id for r in runs})}
    projects = {p: store.get_project(p) for p in {e.project_id for e in experiments.values()}}
    return [
        Delivery(
            hook_id=hook.id, body=WebhookPayload.of(event, run, done_at, experiment, project, hook.link_base)
        )
        for run in runs
        if (done_at := run.done_at) is not None
        and (event := WebhookEvent.of(run)) is not None
        and (experiment := experiments.get(run.experiment_id)) is not None
        and (project := projects[experiment.project_id]) is not None
        for hook in subscribed(hooks.get(project.id, ()), event, run)
    ]


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
        ctx.report(
            **_FindProgress(backlog=0).model_dump(mode="json")
        )  # nothing to announce to, nor a place to start
        return
    # A first look starts where the first webhook was made: what ended before is no one's news. Not "now", which a clock
    # stepped back could put after a run that ended just as it was taken.
    cursor = _progress(ctx).cursor or _Cursor(done_at=min(made), run_id=0)
    before = pendulum.now(pendulum.UTC) - models.RUN_END_SETTLES
    queued = 0
    while not ctx.should_stop():
        with ctx.batch():
            runs = store.runs_done_since((cursor.done_at, cursor.run_id), before=before, limit=_PAGE)
            due = _deliveries(store, runs, hooks)
            tasks.enqueue_work(delivery.queued() for delivery in due)
        queued += len(due)
        if runs:
            last = runs[-1]  # read by its `done_at`, which it has
            cursor = _Cursor(done_at=last.done_at or cursor.done_at, run_id=last.id)
        if len(runs) < _PAGE:
            break
    backlog = tasks.count_work(WebhookWork.DELIVERY)
    ctx.report(**_FindProgress(backlog=backlog, cursor=cursor).model_dump(mode="json"))
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
