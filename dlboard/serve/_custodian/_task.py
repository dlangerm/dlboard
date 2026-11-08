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
from dlboard.serve._backend._app_slot import AppSlot

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Collection, Generator

    from dash import Dash

    from dlboard.serve._custodian._models import TaskState, WorkItem
    from dlboard.serve._custodian._store import TaskStore


_DEFAULT_TIMEOUT: Final = pendulum.duration(minutes=15)


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
    ) -> None:
        self.app = app
        self.holder = holder
        self._task = task
        self._store = store
        self._stop = stop
        self._duty_cycle = duty_cycle
        self._claim_for = claim_for
        self._batches: deque[float] = deque(maxlen=_PACE_HISTORY)
        self.backlog: int | None = None
        """What the task last said it had left to do, for the `dlboard.task.backlog` metric."""

    def should_stop(self) -> bool:
        """Whether the custodian is stopping: a task checks between batches and returns."""
        return self._stop.is_set()

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
        """Let an item that failed go, to be tried again later: a minute, doubling with each attempt, up to a day."""
        wait = pendulum.duration(minutes=min(2 ** max(item.attempts - 1, 0), 24 * 60))
        self._store.retry_work(item.id, error=error, due_at=pendulum.now(pendulum.UTC) + wait)

    @contextmanager
    def batch(self) -> Generator[None]:
        """
        Do one batch of work, then wait long enough to keep this task to its share of the database (`duty_cycle`).

        A batch that took `t` is followed by a wait of `t * (1 - d) / d`: the busier the database, the longer each batch takes,
        and the longer the task leaves it alone. One far slower than the task's usual waits that much longer again.
        """
        started = time.monotonic()  # not the wall clock, which a synced machine steps
        yield
        took = time.monotonic() - started
        wait = took * (1 - self._duty_cycle) / self._duty_cycle
        if len(self._batches) >= _PACE_HISTORY_MIN and took > _SLOW_BATCH * statistics.median(self._batches):
            wait *= _SLOW_BATCH
        self._batches.append(took)
        self._stop.wait(wait)
