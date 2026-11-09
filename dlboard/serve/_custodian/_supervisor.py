"""
The custodian's own loop: runs each registered task on its schedule, in a thread of its own, and watches them.

One `Supervisor` runs in each custodian process. Its main thread never runs a task and never touches the database: it
ticks (`DLBOARD_CUSTODIAN_TICK_S`), keeps the heartbeat the server watches (so a database that is slow, or locked by a
web worker, can never make the server take a working custodian for hung), restarts a task thread that died, and ends
the process when a run passes its task's deadline or the server that started it is gone. A thread of its own does the
database's side: records the custodian's beat, renews the leases and claims its runs hold, and reads the requested runs.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Final

import pendulum
from pydantic import Field, PositiveFloat, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict
from structlog.stdlib import get_logger

from dlboard._compat import StrEnum
from dlboard._telemetry import Attributes, attribute
from dlboard._telemetry.traces import SpanKind, span
from dlboard.serve._custodian._models import TaskOutcome
from dlboard.serve._custodian._task import Task, TaskContext, TaskMode
from dlboard.serve.telemetry import AttributeKey, Counter, Histogram, NoAttributes, Unit, UpDownCounter

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from dash import Dash

    from dlboard.serve._custodian._store import TaskStore

_log = get_logger(__name__)


class CustodianSettings(BaseSettings):
    """`DLBOARD_CUSTODIAN_*` environment variables: the custodian process, and how it paces and watches its tasks."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_CUSTODIAN_")

    enabled: bool = True
    """Whether the server starts a custodian process at all. Off, no background task runs anywhere."""
    tick_s: PositiveFloat = 1.0
    """How often the custodian checks on its tasks and its server, in memory: no database query."""
    poll_s: PositiveFloat = 5.0
    """How often it reads the database for runs asked for."""
    beat_s: PositiveFloat = 15.0
    """How often it records in the database that it is alive."""
    stall_s: PositiveFloat = 30.0
    """How long the server waits for the custodian's heartbeat before it takes it for hung, kills it and starts another."""
    startup_s: PositiveFloat = 600.0
    """How long a custodian may take to start (build the app, and migrate the database if it is first) before it is hung."""
    duty_cycle: float = Field(default=0.25, gt=0, le=1)
    """The share of time a task may spend in a batch of work: after one, it waits for the rest."""
    claim_s: PositiveInt = 300
    """How long a claim on a work item lasts: one whose custodian died is claimed again after this."""


class ExitCode(IntEnum):
    """Why a custodian process ended: the server's watcher logs it, and starts another unless it is stopping."""

    STOPPED = 0
    HUNG = 70
    """A task's run passed its deadline."""
    ORPHANED = 71
    """The server that started it is gone."""


class _Label(StrEnum):
    TASK = "dlboard.task"


@dataclass(frozen=True, slots=True)
class _TaskAttributes(Attributes):
    task: StrEnum = attribute(_Label.TASK)


@dataclass(frozen=True, slots=True)
class _RunAttributes(Attributes):
    task: StrEnum = attribute(_Label.TASK)
    outcome: TaskOutcome = attribute(AttributeKey.OUTCOME)


_RUNS = Counter[_RunAttributes](
    "dlboard.task.runs", Unit.RUNS, "Runs of each background task, by how they ended."
)
_DURATION = Histogram[_TaskAttributes](
    "dlboard.task.duration", Unit.SECONDS, "How long one run of a task took."
)
_BACKLOG = UpDownCounter[_TaskAttributes](
    "dlboard.task.backlog",
    Unit.ITEMS,
    "What each task last said it had left to do (work items, overdue runs).",
)
_STARTS = Counter[NoAttributes]("dlboard.custodian.starts", Unit.WORKERS, "Custodian processes started.")

_LEASE_BEATS: Final = 3
"""How many beats a task's lease outlasts the last one: a custodian that dies holds it only that long."""
_JOIN_S: Final = 5.0
"""How long a stopping custodian waits for its runs to notice and return; one that does not is ended with the process."""
_RESTART_BACKOFF: Final = (1.0, 60.0)
"""How long a task thread that died waits before it is started again: doubling from the first to the second."""


def doubling(first: float, cap: float, times: int) -> float:
    """`first`, doubled `times` times, but never past `cap`: a backoff."""
    return min(first * 2.0 ** max(times, 0), cap)


def custodian_holder() -> str:
    """The current process, as leases and claims name it: `host:pid:tag`, the tag telling apart two starts of one pid."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class _Runner:
    """One task's thread: it waits until it is woken (the task is due, or was asked for), then runs it once."""

    def __init__(self, supervisor: Supervisor, task: Task) -> None:
        self.task = task
        self._supervisor = supervisor
        self._wake = threading.Event()
        self.cancel = threading.Event()
        """Set to end the run in progress: its lease was taken over, or the custodian is stopping. A new one for each run."""
        self.woken = False
        """From the poll waking the runner until its run is over and recorded: the poll wakes a runner once, not twice."""
        self.running_since: float | None = None
        self.thread: threading.Thread | None = None
        self.failures = 0
        self.restart_at = 0.0
        self.backlog_reported = 0
        self.hung = False
        """Set when the supervisor called this run hung: whatever it does after, it records nothing."""

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, daemon=True, name=f"task-{self.task.name}")
        self.thread.start()

    def wake(self) -> None:
        """Run now: the task is due, or a run was asked for."""
        self.woken = True
        self._wake.set()

    def _loop(self) -> None:
        stop = self._supervisor.stop
        while not stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if stop.is_set():
                return
            try:
                self._run_once()
            finally:
                self.woken = False

    def _run_once(self) -> None:
        supervisor, task = self._supervisor, self.task
        store = supervisor.store
        cancel = self.cancel = threading.Event()
        if supervisor.stop.is_set():
            return  # stopping: its lease is let go of next, and a claim now would take it back
        if task.mode is TaskMode.SINGLETON and not store.claim_task(
            task.name, supervisor.holder, until=pendulum.now(pendulum.UTC) + supervisor.lease
        ):
            return  # another custodian holds it: it runs it
        ctx = TaskContext(
            supervisor.app,
            task,
            store,
            supervisor.holder,
            cancel,
            duty_cycle=supervisor.settings.duty_cycle,
            claim_for=pendulum.duration(seconds=supervisor.settings.claim_s),
        )
        store.start_task_run(task.name)
        started = self.running_since = time.monotonic()
        outcome, error = TaskOutcome.SUCCESS, None
        try:
            try:
                with span(f"dlboard.task.{task.name}", SpanKind.INTERNAL):
                    task.run_once(ctx)
            except Exception:  # noqa: BLE001 -- a failed run is recorded, and the task runs again when next due
                outcome, error = TaskOutcome.FAILURE, traceback.format_exc(limit=8)
                _log.exception("A background task failed", task=str(task.name))
            if self.hung:
                return  # the supervisor recorded it as hung, and the process is ending
            store.finish_task_run(task.name, outcome, error=error, progress=None)
        finally:
            # Even if the thread dies: its deadline must not count on for a run that is over.
            self.running_since = None
        _RUNS.add(1, _RunAttributes(task=task.name, outcome=outcome))
        _DURATION.record(time.monotonic() - started, _TaskAttributes(task=task.name))
        if ctx.backlog is not None:
            _BACKLOG.add(ctx.backlog - self.backlog_reported, _TaskAttributes(task=task.name))
            self.backlog_reported = ctx.backlog


class Supervisor:
    """Runs `tasks`, each in a thread of its own, for as long as the server that started this process lives."""

    def __init__(  # noqa: PLR0913 -- what the loop is given, each once
        self,
        app: Dash,
        tasks: Sequence[Task],
        store: TaskStore,
        *,
        settings: CustodianSettings | None = None,
        holder: str | None = None,
        heartbeat: Callable[[], None] = lambda: None,
        parent_alive: Callable[[], bool] = lambda: True,
    ) -> None:
        self.app = app
        self.store = store
        self.settings = settings or CustodianSettings()
        self.holder = holder or custodian_holder()
        self.stop = threading.Event()
        self._heartbeat = heartbeat
        self._parent_alive = parent_alive
        self._started_at = pendulum.now(pendulum.UTC)
        self.lease = pendulum.duration(seconds=self.settings.beat_s * _LEASE_BEATS)
        """
        How long a one-at-a-time task's lease lasts: a few beats, renewed every beat while its run lasts. A custodian
        that dies holds its tasks only that long, and another takes them over.
        """
        self.runners = [_Runner(self, task) for task in tasks]
        self._hung_run: _Runner | None = None

    def run(self) -> ExitCode:
        """Run until stopped, the parent is gone, or a task hangs: why, as the process's exit code."""
        _STARTS.add(1)
        _log.info("Custodian started", holder=self.holder, tasks=[str(r.task.name) for r in self.runners])
        ledger = threading.Thread(target=self._keep_ledger, daemon=True, name="custodian-ledger")
        ledger.start()
        try:
            while not self.stop.is_set():
                self._heartbeat()
                now = time.monotonic()
                if not self._parent_alive():
                    _log.warning("The server that started this custodian is gone; stopping")
                    return ExitCode.ORPHANED
                if (hung := self._hung(now)) is not None:
                    self._hung_run = hung
                    return ExitCode.HUNG
                self._restart_dead(now)
                self.stop.wait(self.settings.tick_s)
            return ExitCode.STOPPED
        finally:
            self.stop.set()
            for runner in self.runners:
                runner.cancel.set()
            # Checked here, not only where the loop noticed: a SIGTERM that arrives because the server died (the kernel's
            # parent-death signal) stops the loop without the orphan check ever running (#580).
            orphaned = not self._parent_alive()
            _log.info("The custodian is stopping", orphaned=orphaned)
            if not orphaned:
                # An orphan exits at once: no server is waiting on it, its leases run out on their own, and a
                # lock held by a write it would wait out (up to the busy timeout) must not keep it alive.
                self._record_hung()
                self._join_runners()
                ledger.join(_JOIN_S)
                self._release()

    def _keep_ledger(self) -> None:
        """
        The database's side of the loop: the tasks' rows, then the beat, the renewals and the requested runs.

        A statement that fails is logged and tried again a tick later: the database being down is not the custodian's to
        die of, and `/healthz/custodian` says no custodian has beaten while it lasts.
        """
        names = [str(runner.task.name) for runner in self.runners]
        while not self.stop.is_set():
            try:
                self.store.ensure_tasks(names)
                break
            except Exception:  # noqa: BLE001 -- as above
                _log.warning("Could not register the tasks in the database; trying again", exc_info=True)
                self.stop.wait(self.settings.poll_s)
        for runner in self.runners:
            runner.start()
        polled = beaten = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            try:
                if now - beaten >= self.settings.beat_s:
                    self.store.beat(
                        self.holder, host=socket.gethostname(), pid=os.getpid(), started_at=self._started_at
                    )
                    self._renew_running()
                    beaten = now
                if now - polled >= self.settings.poll_s:
                    self._wake_due()
                    polled = now
            except Exception:  # noqa: BLE001 -- as above
                _log.warning("Could not reach the database; trying again", exc_info=True)
            self.stop.wait(self.settings.tick_s)

    def _hung(self, now: float) -> _Runner | None:
        """The runner whose run passed its task's deadline, if one did; the process ends, to be started again clean."""
        for runner in self.runners:
            since = runner.running_since
            if since is not None and now - since > runner.task.timeout.total_seconds():
                runner.hung = True
                _log.error(
                    "A background task passed its deadline; restarting the custodian",
                    task=str(runner.task.name),
                )
                return runner
        return None

    def _record_hung(self) -> None:
        """Say in the database that a run hung, if the database lets this be said: the process is ending either way."""
        if (runner := self._hung_run) is None:
            return
        try:
            self.store.finish_task_run(
                runner.task.name,
                TaskOutcome.HUNG,
                error=f"Still running after {runner.task.timeout.in_words()}: its custodian was restarted",
                progress=None,
            )
        except Exception:  # noqa: BLE001 -- stopping must not fail
            _log.warning("Could not record a hung task", task=str(runner.task.name), exc_info=True)

    def _renew_running(self) -> None:
        """
        Extend what the runs in progress hold, a statement each, once a beat: a one-at-a-time task's lease, and the claims.

        A lease another custodian took over (this one stalled past it, or two hosts' clocks disagree) ends its run: it
        is told to stop, and a task that checks `should_stop` between batches returns, so two never run at once.
        """
        running = [runner for runner in self.runners if runner.running_since is not None]
        now = pendulum.now(pendulum.UTC)
        for runner in running:
            if runner.task.mode is TaskMode.SINGLETON and not self.store.claim_task(
                runner.task.name, self.holder, until=now + self.lease
            ):
                _log.warning("A task's lease was taken over; stopping its run", task=str(runner.task.name))
                runner.cancel.set()
        if any(runner.task.mode is TaskMode.SHARED for runner in running):
            self.store.renew_work(self.holder, until=now + pendulum.duration(seconds=self.settings.claim_s))

    def _wake_due(self) -> None:
        """
        Start the tasks that are due, and those a run was asked for: one query for all of them.

        Due is the database's word (`Task.is_due`), on the wall clock, so a task is as late after a sleep as the poll that
        first sees the time gone by. A runner already woken (and so, perhaps, not yet running) is left alone, and the ones
        left are picked before the read, so that one whose run ends in between is not woken by a finish it has not read.
        """
        idle = [runner for runner in self.runners if not runner.woken]
        states = {state.name: state for state in self.store.task_states()}
        now = pendulum.now(pendulum.UTC)
        for runner in self.runners:
            state = states.get(str(runner.task.name))
            asked = state is not None and state.requested_at is not None
            if asked or (runner in idle and runner.task.is_due(state, now, self.holder)):
                runner.wake()

    def _restart_dead(self, now: float) -> None:
        for runner in self.runners:
            if runner.thread is None or runner.thread.is_alive() or self.stop.is_set():
                continue
            if runner.restart_at == 0.0:
                runner.failures += 1
                wait = doubling(*_RESTART_BACKOFF, runner.failures - 1)
                runner.restart_at = now + wait
                _log.error(
                    "A background task's thread died; restarting it", task=str(runner.task.name), after_s=wait
                )
            elif now >= runner.restart_at:
                runner.restart_at = 0.0
                runner.start()

    def _join_runners(self) -> None:
        """Wait (a little) for runs to stop, so that none takes a lease back after it is let go of."""
        deadline = time.monotonic() + _JOIN_S
        for runner in self.runners:
            runner.wake()  # a runner waiting to be asked for is woken, to see the stop
        for runner in self.runners:
            if runner.thread is not None:
                runner.thread.join(max(deadline - time.monotonic(), 0))

    def _release(self) -> None:
        """Let go of every lease this custodian holds, so that another need not wait one out."""
        for runner in self.runners:
            if runner.task.mode is TaskMode.SINGLETON:
                try:
                    self.store.release_task(runner.task.name, self.holder)
                except Exception:  # noqa: BLE001 -- stopping must not fail: the lease runs out on its own
                    _log.warning(
                        "Could not release a task's lease", task=str(runner.task.name), exc_info=True
                    )
