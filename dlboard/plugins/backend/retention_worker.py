"""
Background worker that holds every project to its retention policies, and empties the Trash of what has been in it long enough.

Unlike `artifact_purge_worker`, this one runs on a timer: a run gets older without anyone doing anything, so nothing
could wake it. What keeps it from hammering the database is the rest of its design:

- Every server process (each Granian worker, every replica sharing a database) runs the thread, but only the one holding
  the retention lease (`DataStore.claim_retention_lease`) does anything. The holder renews it after every batch; one
  that dies holds it until it runs out, and then another process takes it over.
- It deletes in batches of `DLBOARD_RETENTION_BATCH`, pausing `DLBOARD_RETENTION_PAUSE_S` between them, and at most
  `DLBOARD_RETENTION_SWEEP_BUDGET` of them a sweep: live traffic gets the database back between batches, and a project
  far past its rule is caught up over several sweeps rather than in one long one.
- It deletes as the retention user, which no one signs in as, so the audit log says who did it.

A run retention deletes goes to the Trash, as one a person deletes does: restorable for `DLBOARD_TRASH_RETENTION_DAYS`,
then purged (with its blobs, by `artifact_purge_worker`) by this worker, whoever deleted it.
"""

from __future__ import annotations

import os
import random
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import pendulum
from pydantic import NonNegativeFloat, PositiveFloat, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict
from structlog.stdlib import get_logger

from dlboard import models
from dlboard._compat import StrEnum
from dlboard._telemetry import Attributes, attribute
from dlboard._telemetry.traces import SpanKind, span
from dlboard.plugins.backend import artifact_purge_worker
from dlboard.serve import wait_for_data_store
from dlboard.serve._backend._retention_settings import OVERDUE_COUNTED, RetentionSettings
from dlboard.serve.telemetry import (
    Counter,
    Histogram,
    NoAttributes,
    Outcome,
    OutcomeAttributes,
    Unit,
    UpDownCounter,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from dash import Dash

    from dlboard.models import DataStore

_log = get_logger(__name__)

RETENTION_PRINCIPAL: Final = models.Principal(
    issuer="dlboard:system", subject="retention", username="dlboard:retention"
)
"""Who retention deletes and purges as: an issuer no auth provider issues, so no one signs in as it."""


class RetentionWorkerSettings(BaseSettings):
    """`DLBOARD_*` environment variables pacing the retention worker."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    retention_enabled: bool = True
    """Whether this server enforces retention at all. Off, policies are still kept, and nothing is deleted or purged."""

    retention_interval_s: PositiveFloat = 300
    """How long the worker waits between one sweep of every project and the next."""

    retention_batch: PositiveInt = 100
    """How many runs (or Trash entries) the worker deletes in one transaction."""

    retention_pause_s: NonNegativeFloat = 1.0
    """How long the worker pauses between batches, so live traffic gets the database back."""

    retention_sweep_budget: PositiveInt = 5_000
    """The most runs (and Trash entries) one sweep deletes: what is left over waits for the next sweep."""

    trash_retention_days: PositiveInt = 30
    """How long anything stays in the Trash (restorable) before the worker purges it for good, whoever deleted it."""


class _Label(StrEnum):
    ENTITY_TYPE = "dlboard.entity.type"


@dataclass(frozen=True, slots=True)
class _EntityAttributes(Attributes):
    entity_type: models.EntityType = attribute(_Label.ENTITY_TYPE)


_DELETED = Counter[NoAttributes](
    "dlboard.retention.runs.deleted", Unit.RUNS, "Runs a retention policy moved to the Trash."
)
_PURGED = Counter[_EntityAttributes](
    "dlboard.trash.purged", Unit.ENTITIES, "What was in the Trash long enough to be purged for good, by kind."
)
_SWEEPS = Histogram[OutcomeAttributes](
    "dlboard.retention.sweep.duration", Unit.SECONDS, "How long one retention sweep of every project took."
)
_OVERDUE = UpDownCounter[NoAttributes](
    "dlboard.retention.runs.overdue",
    Unit.RUNS,
    "Runs retention policies say to delete that are not yet deleted, as of the last sweep (counted up to 10,000 a project).",
)


class _LeaseLost(Exception):  # noqa: N818 -- not an error: another process holds the lease now
    """Another process took the retention lease: this one stops where it is."""


class Sweeper:
    """One process's retention worker: a sweep of every project, as the retention user, if this process holds the lease."""

    def __init__(
        self, store: DataStore[...], holder: str, *, pause: Callable[[float], None] = time.sleep
    ) -> None:
        """A sweeper over `store` that names itself `holder` to the lease, and pauses between batches with `pause`."""
        self._store = store
        self._holder = holder
        """This process, as the retention lease names it."""
        self._pause = pause
        self._settings = RetentionWorkerSettings()
        self._actor = store.get_or_create_user(RETENTION_PRINCIPAL)
        self._overdue_reported = 0
        """How many overdue runs this process last added to `_OVERDUE`: every process's are summed, so it takes them back."""

    def sweep(self) -> None:
        """Hold every project to its policies once, then empty the Trash of what has been in it long enough."""
        try:
            self._renew()
        except _LeaseLost:
            self._report_overdue(0)  # another process reports them now
            return
        started, outcome = time.monotonic(), Outcome.FAILURE
        try:
            self._reconcile()
            budget = self._settings.retention_sweep_budget
            overdue = 0
            for project in list(self._store.get_projects()):
                budget = self._enforce(project.id, budget)
                left = len(self._store.retention_candidates(project.id, limit=OVERDUE_COUNTED))
                self._store.record_retention_status(project.id, overdue_runs=left)
                overdue += left
            self._report_overdue(overdue)
            self._empty_trash(self._store.record_retention_sweep(), budget)
            outcome = Outcome.SUCCESS
        except _LeaseLost:
            self._report_overdue(0)
            outcome = Outcome.SUCCESS  # it stopped because another process took over: nothing failed
        finally:
            _SWEEPS.record(time.monotonic() - started, OutcomeAttributes(outcome=outcome))

    def _renew(self) -> None:
        """Take or renew the lease for long enough to finish a batch however slow, or stop: another process holds it."""
        lease = pendulum.duration(seconds=max(2 * self._settings.retention_interval_s, 600))
        if not self._store.claim_retention_lease(self._holder, until=pendulum.now(pendulum.UTC) + lease):
            raise _LeaseLost

    def _between_batches(self) -> None:
        self._pause(self._settings.retention_pause_s)
        self._renew()

    def _report_overdue(self, overdue: int) -> None:
        _OVERDUE.add(overdue - self._overdue_reported)
        self._overdue_reported = overdue

    def _reconcile(self) -> None:
        """
        Give a project from before retention the server's default, and lower one past a cap an admin did not set to it.

        Both through `set_retention_policy`, as anyone's change is: they take effect once the grace period is over.
        """
        settings = RetentionSettings()
        for project in list(self._store.get_projects()):
            scope = self._store.get_retention(project.id).project
            latest = scope.pending or scope.active
            if latest is None or not (latest.cap_exempt or settings.within_caps(latest.rule)):
                self._store.set_retention_policy(project.id, None, settings.default_rule, self._actor)

    def _enforce(self, project_id: int, budget: int) -> int:
        """Move a project's runs its policies say to delete to the Trash, a batch at a time: what is left of `budget`."""
        while budget > 0:
            run_ids = self._store.retention_candidates(
                project_id, limit=min(self._settings.retention_batch, budget)
            )
            if not run_ids:
                break
            budget -= len(run_ids)
            try:
                self._store.delete_runs(frozenset(run_ids), self._actor)
            except (
                ValueError
            ):  # one was deleted between finding and deleting it: the next batch finds the rest again
                _log.debug("A run retention was deleting was deleted already", project_id=project_id)
            else:
                _DELETED.add(len(run_ids))
                _log.debug("Retention deleted runs", project_id=project_id, runs=len(run_ids))
            self._between_batches()
        return budget

    def _empty_trash(self, since: datetime, budget: int) -> None:
        """Purge what has been in the Trash `DLBOARD_TRASH_RETENTION_DAYS`, counted from no earlier than `since`."""
        kept_for = pendulum.duration(days=self._settings.trash_retention_days)
        now = pendulum.now(pendulum.UTC)
        if now < since + kept_for:
            return  # retention has not run here that long: what was deleted before it started waits its turn too
        for kind in _PURGE_ORDER:
            while budget > 0:
                found = self._store.trashed_before(
                    kind, now - kept_for, limit=min(self._settings.retention_batch, budget)
                )
                if not found:
                    break
                budget -= len(found)
                for entity_id in found:
                    try:
                        self._purge(kind, entity_id)
                    except ValueError:  # restored between finding and purging it
                        continue
                    _PURGED.add(1, _EntityAttributes(entity_type=kind))
                artifact_purge_worker.wake()
                self._between_batches()

    def _purge(self, kind: models.EntityType, entity_id: int) -> None:
        match kind:
            case models.EntityType.PROJECT:
                self._store.purge_project(entity_id, self._actor)
            case models.EntityType.EXPERIMENT:
                self._store.purge_experiment(entity_id, self._actor)
            case models.EntityType.RUN:
                self._store.purge_run(entity_id, self._actor)
            case models.EntityType.ARTIFACT:
                self._store.purge_artifact(entity_id, self._actor)


_PURGE_ORDER: Final = (
    models.EntityType.PROJECT,
    models.EntityType.EXPERIMENT,
    models.EntityType.RUN,
    models.EntityType.ARTIFACT,
)
"""What the Trash is emptied of first: purging a project purges what it holds, so its experiments are never purged one by one."""


def _run(app: Dash) -> None:
    settings = RetentionWorkerSettings()
    sweeper = Sweeper(
        wait_for_data_store(app), f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    )
    # Processes started together (every Granian worker) would otherwise all ask for the lease at the same moment.
    time.sleep(random.uniform(0, settings.retention_interval_s))
    while True:
        try:
            # Nobody's request: its own trace, with the store's statements under it.
            with span("dlboard.retention.sweep", SpanKind.INTERNAL):
                sweeper.sweep()
        except Exception:  # noqa: BLE001 -- the worker outlives a failed sweep: an uncaught one stops every later sweep
            _log.exception("A retention sweep failed; the next one will try again")
        time.sleep(settings.retention_interval_s)


def plug(app: Dash) -> None:
    """Start the background retention worker, unless `DLBOARD_RETENTION_ENABLED` is off."""
    if RetentionWorkerSettings().retention_enabled:
        threading.Thread(target=_run, args=(app,), daemon=True, name="retention-worker").start()
