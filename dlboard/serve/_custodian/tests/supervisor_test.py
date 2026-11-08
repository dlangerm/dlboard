"""The custodian's loop: one runner of a one-at-a-time task, runs asked for, threads restarted, hangs ended, a query budget."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast, override

import pendulum
import pytest

from dlboard.conftest import recorded_statements
from dlboard.serve._custodian import Task, TaskContext, TaskMode, TaskOutcome
from dlboard.serve._custodian._supervisor import CustodianSettings, ExitCode, Supervisor

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from datetime import datetime

    from dash import Dash

    from dlboard.conftest import Clock
    from dlboard.serve import SQLStoreBase


class _Name(StrEnum):
    WORK = "work"


_FAST = CustodianSettings(tick_s=0.01, poll_s=0.05, beat_s=0.1, stall_s=5, duty_cycle=0.5, claim_s=60)


def _task(run_once: Callable[[TaskContext], None], **fields: Any) -> Task:  # noqa: ANN401
    return Task(name=_Name.WORK, run_once=run_once, **fields)


def _supervisor(store: SQLStoreBase[Any], task: Task, holder: str, **kwargs: Any) -> Supervisor:  # noqa: ANN401
    settings = kwargs.pop("settings", _FAST)
    return Supervisor(cast("Dash", object()), [task], store, settings=settings, holder=holder, **kwargs)


@contextmanager
def _running(*supervisors: Supervisor) -> Generator[None]:
    """Run `supervisors` side by side for the block, then stop them (their runs are joined before each returns)."""
    threads = [threading.Thread(target=supervisor.run, daemon=True) for supervisor in supervisors]
    for thread in threads:
        thread.start()
    try:
        yield
    finally:
        for supervisor in supervisors:
            supervisor.stop.set()
        for thread in threads:
            thread.join(timeout=5)


def _state(store: SQLStoreBase[Any]) -> Any:  # noqa: ANN401
    (state,) = store.task_states()
    return state


def _finished(store: SQLStoreBase[Any]) -> bool:
    """Whether a run's end is recorded (the time it is stamped with is the clock's then, so wait before moving it)."""
    return any(state.last_finished_at is not None for state in store.task_states())


def _wait_for(condition: Callable[[], bool], seconds: float = 3) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


@pytest.fixture(autouse=True)
def _stopped(clock: Clock) -> None:
    """Database stamps on a clock that only moves when told to: what is due is the wall clock's word."""


def test_a_one_at_a_time_task_runs_once_an_interval_across_custodians(
    store: SQLStoreBase[Any], clock: Clock
) -> None:
    holders: list[str] = []
    task = _task(
        lambda ctx: holders.append(ctx.holder), every=pendulum.duration(seconds=1), mode=TaskMode.SINGLETON
    )
    with _running(_supervisor(store, task, "a"), _supervisor(store, task, "b")):
        for ran in range(1, 5):
            _wait_for(lambda ran=ran: len(holders) == ran and _finished(store))
            time.sleep(0.2)  # a few polls, to see that no custodian runs it twice
            clock.advance(seconds=1)  # an interval goes by without a moment of the monotonic clock's
    assert len(holders) == 4


def test_a_task_that_came_due_while_the_machine_slept_runs_within_a_poll(
    store: SQLStoreBase[Any], clock: Clock
) -> None:
    """The wall clock moved on (a sleep) and the monotonic one did not: the next poll still sees the task due."""
    ran: list[str] = []
    task = _task(lambda ctx: ran.append(ctx.holder), every=pendulum.duration(hours=1))
    with _running(_supervisor(store, task, "a")):
        _wait_for(lambda: len(ran) == 1 and _finished(store))
        clock.advance(minutes=59)
        time.sleep(0.3)
        assert len(ran) == 1
        clock.advance(hours=3)
        _wait_for(lambda: len(ran) == 2)


def test_a_run_asked_for_starts_within_a_poll(store: SQLStoreBase[Any]) -> None:
    ran = threading.Event()
    with _running(_supervisor(store, _task(lambda _ctx: ran.set()), "a")):
        time.sleep(0.2)
        assert not ran.is_set()  # a task with no interval only runs when asked
        store.request_task_run(_Name.WORK)
        assert ran.wait(timeout=2)
    state = _state(store)
    assert (state.requested_at, state.last_outcome) == (None, TaskOutcome.SUCCESS)


class _Die(BaseException):
    """What kills a task's thread outright: not an `Exception`, which a run catches and records."""


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_task_thread_that_dies_is_started_again(store: SQLStoreBase[Any]) -> None:
    runs: list[int] = []

    def run_once(_ctx: TaskContext) -> None:
        runs.append(1)
        if len(runs) == 1:
            raise _Die

    with _running(_supervisor(store, _task(run_once, every=pendulum.duration(seconds=0.05)), "a")):
        time.sleep(1.6)
    assert len(runs) > 1


def test_a_failed_run_is_recorded_and_the_task_runs_again_when_due(
    store: SQLStoreBase[Any], clock: Clock
) -> None:
    runs: list[int] = []

    def run_once(_ctx: TaskContext) -> None:
        runs.append(1)
        msg = "boom"
        raise ValueError(msg)

    with _running(_supervisor(store, _task(run_once, every=pendulum.duration(minutes=1)), "a")):
        _wait_for(lambda: len(runs) == 1 and _finished(store))
        clock.advance(minutes=2)
        _wait_for(lambda: len(runs) == 2)
    state = _state(store)
    assert state.last_outcome is TaskOutcome.FAILURE
    assert "ValueError: boom" in (state.last_error or "")


def test_a_run_past_its_deadline_ends_the_custodian_and_says_it_hung(store: SQLStoreBase[Any]) -> None:
    def hang(ctx: TaskContext) -> None:
        ctx._stop.wait(5)

    task = _task(hang, timeout=pendulum.duration(seconds=0.1))
    supervisor = _supervisor(store, task, "a")
    store.request_task_run(_Name.WORK)
    assert supervisor.run() is ExitCode.HUNG
    assert _state(store).last_outcome is TaskOutcome.HUNG


def test_a_custodian_whose_server_is_gone_stops(store: SQLStoreBase[Any]) -> None:
    supervisor = _supervisor(store, _task(lambda _ctx: None), "a", parent_alive=lambda: False)
    assert supervisor.run() is ExitCode.ORPHANED


def test_a_custodian_whose_server_is_gone_releases_nothing_and_writes_nothing_as_it_stops(
    store: SQLStoreBase[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody waits on an orphan, and its leases run out by themselves: a database write it waits on must not hold it (#578)."""
    released: list[str] = []

    def release(name: object, _holder: str) -> None:
        released.append(str(name))

    monkeypatch.setattr(store, "release_task", release)
    supervisor = _supervisor(store, _task(lambda _ctx: None), "a", parent_alive=lambda: False)
    assert supervisor.run() is ExitCode.ORPHANED
    assert released == []


def test_a_stop_that_comes_after_the_server_died_releases_nothing_either(
    store: SQLStoreBase[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kernel's parent-death signal stops the loop before its orphan check runs: that stop is an orphan's too (#580)."""
    released: list[str] = []

    def release(name: object, _holder: str) -> None:
        released.append(str(name))

    monkeypatch.setattr(store, "release_task", release)
    supervisor = _supervisor(store, _task(lambda _ctx: None), "a", parent_alive=lambda: False)
    supervisor.stop.set()
    assert supervisor.run() is ExitCode.STOPPED
    assert released == []


def test_a_custodian_lets_go_of_its_leases_when_it_stops(store: SQLStoreBase[Any]) -> None:
    task = _task(lambda _ctx: None, every=pendulum.duration(seconds=0.05), mode=TaskMode.SINGLETON)
    with _running(_supervisor(store, task, "a")):
        time.sleep(0.3)
    assert store.claim_task(_Name.WORK, "b", until=pendulum.now("UTC").add(minutes=1))


def test_a_run_whose_lease_another_custodian_took_over_is_told_to_stop(store: SQLStoreBase[Any]) -> None:
    started, stopped = threading.Event(), threading.Event()

    def run_once(ctx: TaskContext) -> None:
        started.set()
        while not ctx.should_stop():
            time.sleep(0.01)
        stopped.set()

    supervisor = _supervisor(store, _task(run_once, mode=TaskMode.SINGLETON), "a")
    store.request_task_run(_Name.WORK)
    thread = threading.Thread(target=supervisor.run, daemon=True)
    thread.start()
    assert started.wait(timeout=2)
    store.release_task(_Name.WORK, "a")
    assert store.claim_task(_Name.WORK, "b", until=pendulum.now("UTC").add(minutes=1))
    assert stopped.wait(timeout=2), "the run went on after another custodian took its lease"
    assert thread.is_alive()  # the custodian itself carries on
    supervisor.stop.set()
    thread.join(timeout=5)


def test_a_shared_run_keeps_renewing_its_claims_while_it_works(
    store: SQLStoreBase[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    renewed: list[str] = []
    renew = store.renew_work

    def renewing(holder: str, *, until: datetime) -> None:
        renewed.append(holder)
        renew(holder, until=until)

    monkeypatch.setattr(store, "renew_work", renewing)
    task = _task(lambda _ctx: time.sleep(0.6), mode=TaskMode.SHARED)
    store.request_task_run(_Name.WORK)
    with _running(_supervisor(store, task, "a")):
        time.sleep(1.0)
    assert len(renewed) > 1
    assert set(renewed) == {"a"}


def test_the_heartbeat_does_not_wait_for_a_database_that_is_slow(
    store: SQLStoreBase[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write lock held by a web worker blocks the custodian's statements, but not what tells the server it is alive."""

    def slow(holder: str, *, host: str, pid: int, started_at: datetime) -> None:
        time.sleep(1.0)

    monkeypatch.setattr(store, "beat", slow)
    beats: list[int] = []
    supervisor = _supervisor(
        store, _task(lambda _ctx: None), "a", heartbeat=lambda: beats.append(1), parent_alive=lambda: True
    )
    with _running(supervisor):
        time.sleep(0.5)
    assert len(beats) > 20


def test_an_idle_custodian_makes_at_most_thirty_statements_a_minute(store: SQLStoreBase[Any]) -> None:
    """At the default pace (a poll every 5 s, a beat every 15 s), here fifty times faster, over 1.2 s: a minute's worth."""
    settings = CustodianSettings(tick_s=0.01, poll_s=0.1, beat_s=0.3, stall_s=5)
    supervisor = _supervisor(store, _task(lambda _ctx: None), "a", settings=settings)
    with recorded_statements(store._engine) as statements, _running(supervisor):
        time.sleep(1.2)
    assert len(statements) <= 30, statements


class _RecordedWaits(threading.Event):
    """A stop event that never stops, and remembers how long each wait on it was asked to be."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float] = []

    @override
    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout or 0)
        return False


@pytest.mark.parametrize("duty_cycle", [0.25, 0.5, 1.0])
def test_a_batch_is_followed_by_a_wait_that_keeps_the_task_to_its_share_of_time(
    store: SQLStoreBase[Any], duty_cycle: float
) -> None:
    stop = _RecordedWaits()
    ctx = TaskContext(
        cast("Dash", object()),
        _task(lambda _ctx: None),
        store,
        "a",
        stop,
        duty_cycle=duty_cycle,
        claim_for=pendulum.duration(minutes=1),
    )
    for took in (0.01, 0.01, 0.01, 0.2):
        with ctx.batch():
            time.sleep(took)
    expected = [took * (1 - duty_cycle) / duty_cycle for took in (0.01, 0.01, 0.01, 0.2 * 10)]
    assert stop.waits == pytest.approx(expected, rel=0.5, abs=0.01)


def test_a_run_is_told_to_stop_once_it_has_used_its_share_of_the_tasks_timeout(
    store: SQLStoreBase[Any],
) -> None:
    """So that a task that checks between batches ends itself, rather than be found hung and its custodian restarted."""
    ctx = TaskContext(
        cast("Dash", object()),
        _task(lambda _ctx: None, timeout=pendulum.duration(seconds=0.5)),
        store,
        "a",
        threading.Event(),
        duty_cycle=1.0,
        claim_for=pendulum.duration(minutes=1),
    )
    assert not ctx.should_stop()
    time.sleep(0.45)  # past 80% of the half second
    assert ctx.should_stop()
