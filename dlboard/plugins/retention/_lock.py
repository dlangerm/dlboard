"""A project far behind its rule takes no new runs for now: so that it is not filled faster than retention drains it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum

from dlboard import models
from dlboard.plugins.retention._rules import RetentionKey, RetentionStatus
from dlboard.plugins.retention._settings import RetentionSettings
from dlboard.serve.telemetry import Counter, NoAttributes, Unit

if TYPE_CHECKING:
    from dlboard.models import DataStore

_REFUSED = Counter[NoAttributes](
    "dlboard.retention.runs.refused", Unit.RUNS, "Runs a project too far behind its retention rule refused."
)


def refuse_new_runs_while_locked(store: DataStore[...], project_id: int) -> None:
    """
    Refuse a new run in a project that has held runs its rule says to delete for `DLBOARD_RETENTION_LOCK_AFTER_HOURS`.

    Only a new run: one already logging goes on, so no training job dies mid-epoch for it. The project unlocks itself
    when a check catches up (its status then says it holds nothing to delete).
    """
    after = RetentionSettings().lock_after
    if after is None:
        return
    found = store.get_scoped_settings(RetentionKey.STATUS, project_id).project
    status = RetentionStatus.model_validate(found.value) if found is not None else None
    if status is None or not status.is_locked(pendulum.now(pendulum.UTC), after):
        return
    _REFUSED.add(1)
    msg = (
        f"Project {project_id} takes no new runs for now: it has held runs its retention rule says to delete for more "
        "than DLBOARD_RETENTION_LOCK_AFTER_HOURS, and retention is catching up. Runs already logging are not affected."
    )
    raise models.ProjectLockedError(msg)
