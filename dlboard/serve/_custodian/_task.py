"""
A background task, as a plugin declares it: what to run, how often, and whether one custodian or many may run it at once.

A plugin registers its tasks in `plug()` (`register_task`) instead of starting a thread. Every process registers them,
since every process builds the app, but only the custodian process runs them (`Supervisor`): never a web worker.
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import pendulum

from dlboard._compat import StrEnum
from dlboard._telemetry.traces import SpanKind, span
from dlboard.serve._backend._app_slot import AppSlot

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Collection, Generator

    from dash import Dash

    from dlboard.serve._custodian._models import TaskState, WorkItem
    from dlboard.serve._custodian._store import TaskStore


_DEFAULT_TIMEOUT: Final = pendulum.duration(minutes=15)

_RUN_BUDGET: Final = 0.8
"""
The share of its `timeout` a run has before it is told to stop (`should_stop`): a task that checks it between batches ends
itself and goes on in its next run, rather than be found hung, which restarts the whole custodian.
"""


class TaskMode(StrEnum):
    """How many custodians may run a task at once."""

    SINGLETON = "singleton"
    """One, across every server sharing the database: the one holding the task's lease."""
    SHARED = "shared"
    """Any: each claims its own batch of the task's work items (`TaskContext.claim`), so more custodians drain more."""


@dataclass(frozen=True)
class Task:
    """A piece of custodial work: `run_once`, every `every` (or only when asked for, if `None`)."""

    name: StrEnum
    """The task's name, and its label on the `dlboard.task.*` metrics: a value of the plugin's own `StrEnum`."""
    run_once: Callable[[TaskContext], None]
    every: pendulum.Duration | None = None
    mode: TaskMode = TaskMode.SINGLETON
    timeout: pendulum.Duration = _DEFAULT_TIMEOUT
    """
    The longest one run may take. A run that passes it is hung: a thread cannot be stopped, so its custodian records
    it and exits, and the server starts a new one, clean.
    """

    def is_due(self, state: TaskState | None, now: pendulum.DateTime, holder: str) -> bool:
        """
        Whether `holder`'s custodian should run this task at `now`, by what the database holds of its last run.

        A task on an interval is due `every` after its last run finished, on the wall clock: the one every server and the
        admin page agree on, which keeps counting while a machine sleeps (a monotonic one does not). Not due while
        another custodian holds the lease of a one-at-a-time task, or if it never runs on a schedule.
        """
        if self.every is None:
            return False
        if (
            self.mode is TaskMode.SINGLETON
            and state is not None
            and state.holder not in (None, holder)
            and state.lease_until is not None
            and state.lease_until > now
        ):
            return False
        return state is None or state.last_finished_at is None or now >= state.last_finished_at + self.every


TASKS: AppSlot[list[Task]] = AppSlot("background tasks")


def register_task(app: Dash, task: Task) -> None:
    """Declare `task` as `app`'s: the custodian runs it, no web worker does."""
    registered = TASKS.setdefault(app, list)
    if any(t.name == task.name for t in registered):
        msg = f"A background task named {task.name!r} is already registered"
        raise ValueError(msg)
    registered.append(task)


def registered_tasks(app: Dash) -> list[Task]:
    """Every task `app`'s plugins registered."""
    return list(TASKS.find(app) or [])


@dataclass(frozen=True)
class Backoff:
    """How long a work item that failed waits before its next try: `first`, doubling with each try, up to `most`."""

    first: pendulum.Duration
    most: pendulum.Duration

    def after(self, attempts: int) -> pendulum.Duration:
        """The wait after an item's `attempts`-th try failed."""
        # The exponent stops growing long after any wait reaches `most`, so a float can always hold it.
        doubled = self.first.total_seconds() * 2.0 ** min(max(attempts - 1, 0), 64)
        return pendulum.duration(seconds=min(doubled, self.most.total_seconds()))

    def total(self, attempts: int) -> pendulum.Duration:
        """How long an item that fails every time waits in all, from its first try to its `attempts`-th."""
        return sum((self.after(tried) for tried in range(1, attempts)), pendulum.duration())


_PACE_HISTORY: Final = 20
"""How many of a task's latest batches its pacing compares a batch with."""
_PACE_HISTORY_MIN: Final = 3
"""How many batches a task must have done before one is called slow against them."""
_SLOW_BATCH: Final = 10
"""A batch this many times its task's median is the database under strain: the task waits that much longer again."""


class TaskContext:
    """What a running task is given: the app (for its stores), its lease, the queue, and pacing."""

    def __init__(  # noqa: PLR0913 -- what a run needs, each once
        self,
        app: Dash,
        task: Task,
        store: TaskStore,
        holder: str,
        stop: threading.Event,
        *,
        duty_cycle: float,
        claim_for: pendulum.Duration,
        backoff: Backoff,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.holder = holder
        self._task = task
        self._store = store
        self._stop = stop
        self._duty_cycle = duty_cycle
        self._claim_for = claim_for
        self._backoff = backoff
        self._clock = clock
        """What batches and the run's budget are timed by, in seconds. Monotonic, not the wall clock, which a synced machine steps; a test hands it its own."""
        self._batches: deque[float] = deque(maxlen=_PACE_HISTORY)
        self._rest_owed = 0.0
        """How long the last batch asks the task to rest before its next (`batch`)."""
        self._ends_at = clock() + task.timeout.total_seconds() * _RUN_BUDGET
        self.backlog: int | None = None
        """What the task last said it had left to do, for the `dlboard.task.backlog` metric."""

    def should_stop(self) -> bool:
        """
        Whether the custodian is stopping, or this run has used its budget of the task's timeout: check between batches and return.

        Rests first for what the last batch owes, so a stop asked for while resting is answered before the next batch.
        """
        self._rest()
        return self._stop.is_set() or self._clock() > self._ends_at

    def _rest(self) -> None:
        """Wait out what the last batch owes, once, as a span of its own: a trace would show only a gap where it was."""
        if self._rest_owed > 0:
            with span("dlboard.custodian.rest", SpanKind.INTERNAL):
                self._stop.wait(self._rest_owed)
            self._rest_owed = 0.0

    def report(self, *, backlog: int | None = None, **details: Any) -> None:  # noqa: ANN401 -- the task's own JSON
        """Record what the task is doing (how much is left), shown on the admin page and as `dlboard.task.backlog`."""
        self.backlog = backlog
        self._store.record_task_progress(self._task.name, {"backlog": backlog, **details})

    def claim(self, kind: str, *, limit: int) -> list[WorkItem]:
        """Claim up to `limit` due items of `kind` for this custodian: another custodian claiming at once gets others."""
        return self._store.claim_work(
            kind, self.holder, limit=limit, until=pendulum.now(pendulum.UTC) + self._claim_for
        )

    def done(self, items: Collection[WorkItem]) -> None:
        """Take finished items off the queue."""
        self._store.complete_work([item.id for item in items])

    def retry(self, item: WorkItem, error: str) -> None:
        """Let an item that failed go, to be tried again after the custodian's `Backoff` for its attempts so far."""
        self._store.retry_work(
            item.id, error=error, due_at=pendulum.now(pendulum.UTC) + self._backoff.after(item.attempts)
        )

    @contextmanager
    def batch(self) -> Generator[None]:
        """
        Do one batch of work, resting first long enough to keep this task to its share of the database (`duty_cycle`).

        A batch that took `t` owes a rest of `t * (1 - d) / d`: the busier the database, the longer each batch takes, and the
        longer the task leaves it alone. One far slower than the task's usual owes that much longer again. The rest comes
        before the next batch (or `should_stop`), not after this one: nothing follows a run's last batch, so its run ends
        with its work instead of holding its lease, and whatever it asked for, to rest.
        """
        self._rest()
        started = self._clock()
        yield
        took = self._clock() - started
        rest = took * (1 - self._duty_cycle) / self._duty_cycle
        if len(self._batches) >= _PACE_HISTORY_MIN and took > _SLOW_BATCH * statistics.median(self._batches):
            rest *= _SLOW_BATCH
        self._batches.append(took)
        self._rest_owed = rest
