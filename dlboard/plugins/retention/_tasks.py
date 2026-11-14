"""
Retention's two background tasks: one schedules a check of each project with a rule, the other does the checks.

- **Scheduling** (one at a time, every `DLBOARD_RETENTION_INTERVAL_S`) gives a project from before retention the server's
  default, lowers one past a lowered cap (both through the grace period, as any change), and queues a check of every
  project held to more than every run: one work item a project, so a queued check is never queued twice.
- **Checking** (any custodian, claiming one project at a time) deletes what a project's rules say to, a batch to a
  transaction and paced to its share of the database, up to `DLBOARD_RETENTION_PROJECT_BUDGET` a check, then records how
  the project stands. Several custodians check different projects at once.

Runs go to the Trash, as the `dlboard:retention` user no one signs in as; the Trash empties itself later.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Final

import pendulum
from structlog.stdlib import get_logger

from dlboard import models
from dlboard._compat import StrEnum
from dlboard.plugins.retention._candidates import Candidates, Rules
from dlboard.plugins.retention._rules import RetentionKey, RetentionSetting, RetentionStatus, Unlimited
from dlboard.plugins.retention._settings import OVERDUE_COUNTED, RetentionSettings
from dlboard.serve import get_system_data_store
from dlboard.serve._custodian import NewWorkItem, get_system_task_store
from dlboard.serve.telemetry import Counter, NoAttributes, Unit

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from dlboard.models import DataStore
    from dlboard.serve import TaskContext

_log = get_logger(__name__)

_DELETED = Counter[NoAttributes](
    "dlboard.retention.runs.deleted", Unit.RUNS, "Runs retention moved to the Trash."
)

RETENTION_PRINCIPAL: Final = models.Principal.system("retention")
"""Who retention deletes as."""


class RetentionTask(StrEnum):
    """The plugin's tasks."""

    SCHEDULE = "retention_schedule"
    CHECK = "retention_check"


class RetentionWork(StrEnum):
    """The plugin's work items. Stored as their kind, so a value is never renamed or removed."""

    CHECK = "retention_check"
    """A project to check against its rules: its id is the item's key."""


def rules_from(
    project: RetentionSetting, experiments: Mapping[int, models.ScopedSetting], now: datetime
) -> Rules:
    """The rules a project and its experiments are held to at `now`, from their settings."""
    return Rules(
        project=project.in_effect(now) or Unlimited(),
        experiments={
            e: rule
            for e, s in experiments.items()
            if (rule := RetentionSetting.model_validate(s.value).in_effect(now)) is not None
        },
    )


def _project_setting(found: models.ScopedSettings) -> RetentionSetting:
    return RetentionSetting.model_validate(found.project.value) if found.project else RetentionSetting()


def rules_of(store: DataStore[...], project_id: int, now: datetime) -> Rules:
    """The rules a project and its experiments are held to at `now`."""
    found = store.get_scoped_settings(RetentionKey.RULE, project_id)
    return rules_from(_project_setting(found), found.experiments, now)


def _reconciled(
    store: DataStore[...],
    project_id: int,
    current: RetentionSetting,
    asker: models.User,
    settings: RetentionSettings,
) -> RetentionSetting:
    """
    `current`, or what it becomes (saved) if it has no rule while the server has a default, or keeps more than a cap.

    A project is lowered after the grace period, like any change, unless an admin set it past the cap on purpose. Decided
    from `current` alone: only a project that changes costs a statement.
    """
    latest = current.latest()
    if latest is not None and (latest.cap_exempt or settings.within_caps(latest.rule)):
        return current
    default = settings.default_rule
    if latest is None and isinstance(default, Unlimited):
        return current  # every run kept, as it always was: nothing to save
    changed = current.saved(default, by=asker.id, now=pendulum.now(pendulum.UTC), grace=settings.grace)
    store.put_scoped_setting(RetentionKey.RULE, project_id, None, changed.dump(), asker)
    return changed


_SWEEP_BATCH: Final = 500
"""How many projects one batch of scheduling goes through before it pauses (and looks whether it is to stop)."""


def schedule_checks(ctx: TaskContext) -> None:
    """
    Reconcile projects with the server's default and caps, and queue a check of each held to more than every run.

    One read gives every project's rule setting. A project with none, when the server has no default, is not so much as
    looked at, so what an idle server pays is that read and nothing for each project; a project with a rule costs a
    statement only if it changes, and the checks are queued a batch of projects at a time.
    """
    store, tasks = get_system_data_store(ctx.app), get_system_task_store(ctx.app)
    settings = RetentionSettings()
    asker = store.get_or_create_user(RETENTION_PRINCIPAL)
    stored = store.all_scoped_settings(RetentionKey.RULE)
    # A project with no rule only matters if there is a default to give it: else it keeps every run, as it always did.
    project_ids = (
        list(stored)
        if isinstance(settings.default_rule, Unlimited)
        else [project.id for project in store.get_projects()]
    )
    queued = 0
    for batch in itertools.batched(project_ids, _SWEEP_BATCH):
        if ctx.should_stop():
            break
        with ctx.batch():
            now = pendulum.now(pendulum.UTC)
            due: list[NewWorkItem] = []
            for project_id in batch:
                found = stored.get(project_id, models.ScopedSettings())
                project = _reconciled(store, project_id, _project_setting(found), asker, settings)
                if not rules_from(project, found.experiments, now).keeps_everything:
                    due.append(NewWorkItem(kind=RetentionWork.CHECK, key=str(project_id)))
            tasks.enqueue_work(due)
            queued += len(due)
    if queued:
        tasks.request_task_run(RetentionTask.CHECK)
    ctx.report(backlog=tasks.count_work(RetentionWork.CHECK), projects=queued)


def _check(ctx: TaskContext, project_id: int, asker: models.User) -> int:
    """Delete what a project's rules say to, up to its budget; record how it stands; return how many runs are left."""
    store, settings = get_system_data_store(ctx.app), RetentionSettings()
    now = pendulum.now(pendulum.UTC)
    candidates = Candidates(store, settings, now)
    with ctx.batch():
        # Where each experiment is cut, once: deleting what is past a cutoff moves none, so every batch reads against these.
        cutoffs = candidates.cutoffs(project_id, rules_of(store, project_id, now))
    budget = settings.retention_project_budget
    caught_up = False
    while budget > 0 and not ctx.should_stop():
        with ctx.batch():
            run_ids = candidates.read(cutoffs, limit=min(settings.retention_batch, budget))
            if run_ids:
                try:
                    # A run kept since it was found is spared by the statement itself, not by a read before it.
                    _DELETED.add(len(store.delete_runs(frozenset(run_ids), asker, spare_kept=True)))
                except ValueError:  # one was deleted between finding and deleting it: found again next batch
                    _log.debug("A run retention was deleting was deleted already", project_id=project_id)
        if not run_ids:
            caught_up = True
            break
        budget -= len(run_ids)
    now = pendulum.now(pendulum.UTC)
    # Nothing found is nothing left; else a database count (bounded: past it, "or more"), never a read of the runs.
    left = 0 if caught_up else candidates.count(cutoffs, limit=OVERDUE_COUNTED)
    found = store.get_scoped_settings(RetentionKey.STATUS, project_id).project
    before = RetentionStatus.model_validate(found.value) if found is not None else None
    since = None
    if left:
        since = (before.out_of_spec_since if before is not None else None) or now
    status = RetentionStatus(checked_at=now, overdue_runs=left, out_of_spec_since=since)
    store.put_scoped_setting(RetentionKey.STATUS, project_id, None, status.model_dump(mode="json"), asker)
    return left


def run_checks(ctx: TaskContext) -> None:
    store = get_system_data_store(ctx.app)
    asker = store.get_or_create_user(RETENTION_PRINCIPAL)
    overdue = 0
    while not ctx.should_stop():
        claimed = ctx.claim(RetentionWork.CHECK, limit=1)
        if not claimed:
            break
        (item,) = claimed
        try:
            overdue += _check(ctx, int(item.key), asker)
        except Exception as exc:  # noqa: BLE001 -- one project's failure must not stop the others' checks
            _log.warning("A project's retention check failed", project_id=item.key, exc_info=True)
            ctx.retry(item, f"{type(exc).__name__}: {exc}")
        else:
            ctx.done(claimed)
    ctx.report(backlog=overdue)
