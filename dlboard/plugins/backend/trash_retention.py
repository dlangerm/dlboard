"""
The Trash empties itself: what has been in it `DLBOARD_TRASH_RETENTION_DAYS` is purged for good, whoever deleted it.

A background task, once an hour, queues a purge (`PurgeWork.ENTITY`) for each entity that has been in the Trash that long,
as the `dlboard:trash` user, whom no one signs in as; `artifact_purge_worker`'s task does the purging, as it does for an
admin's. The days count from no earlier than this task's first run, kept in its own progress, so upgrading to a version
with it never purges at once what was deleted long before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pendulum
from pydantic import PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard import models
from dlboard._compat import StrEnum
from dlboard.plugins.backend.artifact_purge_worker import PurgeTask
from dlboard.serve import get_system_data_store
from dlboard.serve._backend._purge_work import PurgeWork, entity_purge
from dlboard.serve._custodian import Task, get_system_task_store, register_task

if TYPE_CHECKING:
    from dash import Dash

    from dlboard.serve._custodian import TaskContext

TRASH_PRINCIPAL: Final = models.Principal(issuer="dlboard:system", subject="trash", username="dlboard:trash")
"""Who the Trash's own purges are asked for as: an issuer no auth provider issues, so no one signs in as it."""

_ORDER: Final = (
    models.EntityType.PROJECT,
    models.EntityType.EXPERIMENT,
    models.EntityType.RUN,
    models.EntityType.ARTIFACT,
)
"""What is queued first: purging a project purges what it holds, so its runs need no purge of their own."""
_PER_RUN: Final = 1_000
"""How many entities of each kind one run queues: the rest are queued on the next, once these are purged."""


class TrashRetentionSettings(BaseSettings):
    """`DLBOARD_TRASH_RETENTION_DAYS`: how long the Trash keeps what is in it."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    trash_retention_days: PositiveInt = 30
    """How long anything stays in the Trash, restorable, before it is purged for good, whoever deleted it."""


class TrashTask(StrEnum):
    """The plugin's task."""

    RETENTION = "trash_retention"


def _since(ctx: TaskContext) -> pendulum.DateTime:
    """
    When this task first ran on this database: its own progress says (every run writes it back), else it is now.

    Progress is for display, so anything that rewrote it without `since` would restart the count: deliberately the
    safe way to fail, since the Trash then keeps what is in it longer, never shorter.
    """
    states = get_system_task_store(ctx.app).task_states()
    state = next((s for s in states if s.name == TrashTask.RETENTION), None)
    recorded = state.progress.get("since") if state is not None else None
    if isinstance(recorded, str):
        parsed = pendulum.parse(recorded)
        if isinstance(parsed, pendulum.DateTime):
            return parsed
    return pendulum.now(pendulum.UTC)


def _empty_trash(ctx: TaskContext) -> None:
    store, tasks = get_system_data_store(ctx.app), get_system_task_store(ctx.app)
    since = _since(ctx)
    kept_for = pendulum.duration(days=TrashRetentionSettings().trash_retention_days)
    now = pendulum.now(pendulum.UTC)
    queued = 0
    if now - since >= kept_for:
        asker = store.get_or_create_user(TRASH_PRINCIPAL)
        for kind in _ORDER:
            with ctx.batch():
                due = store.trashed_before(kind, now - kept_for, limit=_PER_RUN)
                tasks.enqueue_work(entity_purge(kind, entity_id, requested_by=asker.id) for entity_id in due)
            queued += len(due)
        if queued:
            tasks.request_task_run(PurgeTask.ENTITIES)
    ctx.report(backlog=tasks.count_work(PurgeWork.ENTITY), since=since.isoformat(), queued=queued)


def plug(app: Dash) -> None:
    """Register the hourly task that queues the purge of what has been in the Trash long enough."""
    register_task(
        app, Task(name=TrashTask.RETENTION, run_once=_empty_trash, every=pendulum.duration(hours=1))
    )
