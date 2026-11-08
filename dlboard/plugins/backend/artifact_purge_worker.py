"""
Purging, as background tasks: entities an admin asked to purge, and the artifact blobs each purge leaves behind.

Both are `SHARED` tasks over work items (`PurgeWork`): every custodian may run them, each claiming its own batch, so
several servers sharing a database purge in parallel without doing anything twice. Work queued before a crash is still
queued after it, and a task picks it up on its next run: nothing waits for an admin to resume it.

- **An entity purge** is queued by the admin page (as the admin who asked: `PURGE`). The task purges it as them, so the
  audit log names them, and the purge queues a deletion for each blob it orphans, in its own transaction.
- **A blob deletion** deletes one blob from the artifact store. One that fails is tried again later, backing off.
"""

from __future__ import annotations

import functools
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import pendulum
from pydantic import AnyUrl
from structlog.stdlib import get_logger

from dlboard import models
from dlboard._compat import StrEnum
from dlboard.models import Scope
from dlboard.serve import get_system_artifact_store, get_system_data_store
from dlboard.serve._backend._purge_work import REQUESTED_BY, BlobDeletion, EntityPurge, PurgeWork
from dlboard.serve._custodian import (
    Task,
    TaskMode,
    WorkRule,
    get_system_task_store,
    register_task,
    register_work_kind,
)
from dlboard.serve.telemetry import Counter, Outcome, OutcomeAttributes, Unit

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from dash import Dash

    from dlboard.models import DataStore
    from dlboard.serve._custodian import TaskContext, WorkItem

_log = get_logger(__name__)

_PURGES = Counter[OutcomeAttributes](
    "dlboard.artifact.purges",
    Unit.ARTIFACTS,
    "Purged artifacts' blobs deleted from the artifact store, or that could not be.",
)

_BATCH: Final = 100
"""How many items one batch claims."""
_EVERY: Final = pendulum.duration(seconds=30)
"""How often each task looks for work it was not asked to do: what a crash left queued, say."""


class PurgeTask(StrEnum):
    """The plugin's tasks."""

    BLOBS = "artifact_blob_deletion"
    ENTITIES = "trash_purge"


@dataclass(frozen=True)
class _Drained:
    done: int = 0
    failed: int = 0


def _drain(
    ctx: TaskContext, kind: PurgeWork, act: Callable[[WorkItem], None], *, act_paces: bool = False
) -> _Drained:
    """
    Claim `kind`'s due items a batch at a time, `act` on each, until none is left or the custodian stops.

    A batch is followed by the rest the duty cycle asks for, unless `act` rests between chunks of its own (`act_paces`):
    resting for the claim as well would make every item wait twice, and past the task's deadline for a very large one.
    """
    done = failed = 0
    while not ctx.should_stop():
        with nullcontext() if act_paces else ctx.batch():
            items = ctx.claim(kind, limit=_BATCH)
            finished: list[WorkItem] = []
            for item in items:
                try:
                    act(item)
                except InterruptedError:
                    # Not a failure: the run must stop. The item stays claimed, and is claimed again when its claim runs out.
                    _log.info("An item goes on in a later run", kind=str(kind), key=item.key)
                except Exception as exc:  # noqa: BLE001 -- one bad item must not stop the rest of the batch
                    _log.warning(
                        "A purge item failed; it will be tried again",
                        kind=str(kind),
                        key=item.key,
                        exc_info=True,
                    )
                    ctx.retry(item, f"{type(exc).__name__}: {exc}")
                    failed += 1
                else:
                    finished.append(item)
            ctx.done(finished)
            done += len(finished)
        if not items:
            break
    ctx.report(backlog=get_system_task_store(ctx.app).count_work(kind), done=done, failed=failed)
    return _Drained(done, failed)


def _delete_blobs(ctx: TaskContext) -> None:
    artifact_store = get_system_artifact_store(ctx.app)

    def delete(item: WorkItem) -> None:
        deletion = BlobDeletion.model_validate(item.payload)
        try:
            artifact_store.delete_artifact(AnyUrl(deletion.ref))
        except Exception:
            _PURGES.add(1, OutcomeAttributes(outcome=Outcome.FAILURE))
            raise
        _PURGES.add(1, OutcomeAttributes(outcome=Outcome.SUCCESS))

    drained = _drain(ctx, PurgeWork.BLOB, delete)
    if drained.done or drained.failed:
        _log.info("Deleted purged artifacts' blobs", deleted=drained.done, failed=drained.failed)


@contextmanager
def _chunk_of_a_purge(ctx: TaskContext) -> Generator[None]:
    """One chunk of a purge, then the duty cycle's rest. A run that must stop ends the purge, which goes on in the next."""
    if ctx.should_stop():
        msg = "the custodian is stopping, or this run is out of time; the purge goes on in the next"
        raise InterruptedError(msg)
    with ctx.batch():
        yield


def _purge(store: DataStore[...], purge: EntityPurge, actor: models.User, pace: models.Pace) -> None:
    match purge.entity_type:
        case models.EntityType.PROJECT:
            store.purge_project(purge.entity_id, actor, pace=pace)
        case models.EntityType.EXPERIMENT:
            store.purge_experiment(purge.entity_id, actor, pace=pace)
        case models.EntityType.RUN:
            store.purge_run(purge.entity_id, actor, pace=pace)
        case models.EntityType.ARTIFACT:
            store.purge_artifact(purge.entity_id, actor)


def _purge_entities(ctx: TaskContext) -> None:
    store = get_system_data_store(ctx.app)

    def purge(item: WorkItem) -> None:
        asked = EntityPurge.model_validate(item.payload)
        actor = store.get_user(asked.requested_by)
        if actor is None:
            msg = f"user {asked.requested_by}, who asked for the purge, is gone"
            raise LookupError(msg)
        try:
            _purge(store, asked, actor, functools.partial(_chunk_of_a_purge, ctx))
        except ValueError:  # restored since it was asked for: there is nothing to purge
            _log.info("Not purging what was restored", entity=item.key)

    drained = _drain(ctx, PurgeWork.ENTITY, purge, act_paces=True)
    if drained.done:
        _log.info("Purged from the Trash", purged=drained.done, failed=drained.failed)
        get_system_task_store(ctx.app).request_task_run(PurgeTask.BLOBS)  # the blobs they left, at once


def plug(app: Dash) -> None:
    """Register the two purge tasks, and let an admin's request queue an entity purge."""
    register_task(app, Task(name=PurgeTask.BLOBS, run_once=_delete_blobs, every=_EVERY, mode=TaskMode.SHARED))
    register_task(
        app, Task(name=PurgeTask.ENTITIES, run_once=_purge_entities, every=_EVERY, mode=TaskMode.SHARED)
    )
    register_work_kind(
        app,
        WorkRule(kind=PurgeWork.ENTITY, scope=Scope.PURGE, task=PurgeTask.ENTITIES, actor_field=REQUESTED_BY),
    )
