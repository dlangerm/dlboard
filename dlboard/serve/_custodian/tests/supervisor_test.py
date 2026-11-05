"""The custodian's loop: one runner of a one-at-a-time task, runs asked for, threads restarted, hangs ended, a query budget."""

from __future__ import annotations

import threading
import time
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast, override

import pendulum
import pytest
from sqlalchemy import event

from dlboard.serve._custodian import Task, TaskContext, TaskMode, TaskOutcome
from dlboard.serve._custodian._supervisor import CustodianSettings, ExitCode, Supervisor

if TYPE_CHECKING:
    from collections.abc import Callable
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


def _for(seconds: float, *supervisors: Supervisor) -> list[ExitCode]:
    """Run `supervisors` side by side for `seconds`, then stop them: how each ended."""
    codes: dict[int, ExitCode] = {}
    threads = [
        threading.Thread(target=lambda i=i, s=s: codes.__setitem__(i, s.run()), daemon=True)
        for i, s in enumerate(supervisors)
    ]
    for thread in threads:
        thread.start()
    time.sleep(seconds)
    for supervisor in supervisors:
        supervisor.stop.set()
    for thread in threads:
        thread.join(timeout=5)
    return [codes[i] for i in range(len(supervisors))]


def _state(store: SQLStoreBase[Any]) -> Any:  # noqa: ANN401
    (state,) = store.task_states()
    return state


@pytest.fixture(autouse=True)
def _stopped(clock: Clock) -> None:
    """Database stamps on a clock that only moves when told to (a run's timing is the monotonic clock's)."""


def test_a_one_at_a_time_task_is_run_by_one_custodian_of_several(store: SQLStoreBase[Any]) -> None:
    holders: list[str] = []
    task = _task(lambda ctx: holders.append(ctx.holder), every=pendulum.duration(seconds=0.05))
    _for(0.6, _supervisor(store, task, "a"), _supervisor(store, task, "b"))
    assert len(holders) > 2
    assert len(set(holders)) == 1


def test_a_run_asked_for_starts_within_a_poll(store: SQLStoreBase[Any]) -> None:
    ran = threading.Event()
    supervisor = _supervisor(store, _task(lambda _ctx: ran.set()), "a")
    thread = threading.Thread(target=supervisor.run, daemon=True)
    thread.start()
    time.sleep(0.2)
    assert not ran.is_set()  # a task with no interval only runs when asked
    store.request_task_run(_Name.WORK)
    assert ran.wait(timeout=2)
    supervisor.stop.set()
    thread.join(timeout=5)  # its runs are joined before it returns: the finish is recorded
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

    _for(1.6, _supervisor(store, _task(run_once, every=pendulum.duration(seconds=0.05)), "a"))
    assert len(runs) > 1


def test_a_failed_run_is_recorded_and_the_task_runs_again(store: SQLStoreBase[Any]) -> None:
    def run_once(_ctx: TaskContext) -> None:
        msg = "boom"
        raise ValueError(msg)

    _for(0.3, _supervisor(store, _task(run_once, every=pendulum.duration(seconds=0.05)), "a"))
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


def test_a_custodian_lets_go_of_its_leases_when_it_stops(store: SQLStoreBase[Any]) -> None:
    task = _task(lambda _ctx: None, every=pendulum.duration(seconds=0.05), mode=TaskMode.SINGLETON)
    _for(0.3, _supervisor(store, task, "a"))
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
    _for(1.0, _supervisor(store, task, "a"))
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
    _for(0.5, supervisor)
    assert len(beats) > 20


def test_an_idle_custodian_makes_at_most_thirty_statements_a_minute(store: SQLStoreBase[Any]) -> None:
    """At the default pace (a poll every 5 s, a beat every 15 s), here fifty times faster, over 1.2 s: a minute's worth."""
    statements: list[str] = []

    def record(*args: Any) -> None:  # noqa: ANN401
        if not args[2].startswith("BEGIN"):  # SQLite's own start of a transaction: not a query
            statements.append(args[2])

    event.listen(store._engine, "before_cursor_execute", record)
    settings = CustodianSettings(tick_s=0.01, poll_s=0.1, beat_s=0.3, stall_s=5)
    try:
        _for(1.2, _supervisor(store, _task(lambda _ctx: None), "a", settings=settings))
    finally:
        event.remove(store._engine, "before_cursor_execute", record)
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
