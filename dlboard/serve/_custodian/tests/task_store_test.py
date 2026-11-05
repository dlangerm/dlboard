"""The task store on every backend: one holder a lease, a request answered once, and work items claimed without overlap."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import pendulum
import pytest
import sqlalchemy as sa

from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend
from dlboard.serve._custodian import NewWorkItem, TaskOutcome, WorkItem

if TYPE_CHECKING:
    from dlboard.conftest import Clock
    from dlboard.serve import SQLStoreBase


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


@pytest.fixture(autouse=True)
def _stopped(clock: Clock, store: SQLStoreBase[Any]) -> None:
    """On a clock that only moves when told to, with the tasks a custodian registers as it starts."""
    store.ensure_tasks(["sweep", "other"])


def _later(**by: float) -> pendulum.DateTime:
    return pendulum.now("UTC") + pendulum.duration(**by)


def test_only_one_holder_has_a_task_until_its_lease_runs_out_or_it_lets_go(
    store: SQLStoreBase[Any], clock: Clock
) -> None:
    assert store.claim_task("sweep", "a", until=_later(minutes=5))
    assert not store.claim_task("sweep", "b", until=_later(minutes=5))
    assert store.claim_task("sweep", "a", until=_later(minutes=10))
    assert store.claim_task("other", "b", until=_later(minutes=5))
    clock.advance(minutes=11)
    assert store.claim_task("sweep", "b", until=_later(minutes=5))
    store.release_task("sweep", "b")
    assert store.claim_task("sweep", "a", until=_later(minutes=5))


def test_a_run_answers_the_request_made_before_it_started_and_not_one_made_after(
    store: SQLStoreBase[Any], clock: Clock
) -> None:
    store.request_task_run("sweep")
    store.start_task_run("sweep")
    (state,) = (s for s in store.task_states() if s.name == "sweep")
    assert state.requested_at is None
    store.request_task_run("sweep")  # during the run: needs a run of its own
    store.finish_task_run("sweep", TaskOutcome.SUCCESS, error=None, progress={"backlog": 3})
    (state,) = (s for s in store.task_states() if s.name == "sweep")
    assert state.requested_at is not None
    assert (state.last_outcome, state.progress, state.last_succeeded_at) == (
        TaskOutcome.SUCCESS,
        {"backlog": 3},
        state.last_finished_at,
    )
    clock.advance(seconds=1)
    store.start_task_run("sweep")
    store.finish_task_run("sweep", TaskOutcome.FAILURE, error="boom", progress=None)
    (state,) = (s for s in store.task_states() if s.name == "sweep")
    assert (state.requested_at, state.last_error, state.progress) == (None, "boom", {"backlog": 3})
    assert state.last_succeeded_at != state.last_finished_at


def test_a_request_for_a_task_no_custodian_has_registered_yet_is_kept(store: SQLStoreBase[Any]) -> None:
    store.request_task_run("new")
    (state,) = (s for s in store.task_states() if s.name == "new")
    assert state.requested_at is not None


def test_custodians_are_listed_while_they_beat_and_forgotten_a_week_after(
    store: SQLStoreBase[Any], clock: Clock
) -> None:
    started = pendulum.now("UTC")
    store.beat("a", host="h", pid=1, started_at=started)
    store.beat("b", host="h", pid=2, started_at=started)
    clock.advance(minutes=1)
    store.beat("a", host="h", pid=1, started_at=started)
    assert [c.holder for c in store.custodians(seen_since=_later(seconds=-30))] == ["a"]
    clock.advance(days=8)
    store.beat("c", host="h", pid=3, started_at=pendulum.now("UTC"))
    assert [c.holder for c in store.custodians(seen_since=_later(days=-30))] == ["c"]


def _queue(store: SQLStoreBase[Any], count: int, kind: str = "blob") -> None:
    store.enqueue_work(NewWorkItem(kind=kind, key=str(i), payload={"i": i}) for i in range(count))


def test_work_is_queued_once_a_key_and_claimed_in_batches_that_never_overlap(
    store: SQLStoreBase[Any],
) -> None:
    _queue(store, 5)
    _queue(store, 5)
    _queue(store, 2, kind="other")
    assert store.count_work("blob") == 5
    first = store.claim_work("blob", "a", limit=3, until=_later(minutes=5))
    second = store.claim_work("blob", "b", limit=3, until=_later(minutes=5))
    assert ([w.key for w in first], [w.key for w in second]) == (["0", "1", "2"], ["3", "4"])
    assert {w.claimed_by for w in first} == {"a"}
    assert store.claim_work("blob", "c", limit=3, until=_later(minutes=5)) == []
    store.complete_work([w.id for w in first])
    assert store.count_work("blob") == 2


def test_a_claim_that_runs_out_is_claimed_again_and_a_retried_item_waits_its_turn(
    store: SQLStoreBase[Any], clock: Clock
) -> None:
    _queue(store, 2)
    (dead, failed) = store.claim_work("blob", "a", limit=2, until=_later(minutes=5))
    store.retry_work(failed.id, error="boom", due_at=_later(hours=1))
    clock.advance(minutes=6)
    (again,) = store.claim_work("blob", "b", limit=5, until=_later(minutes=5))
    assert (again.key, again.attempts, again.claimed_by) == (dead.key, 2, "b")
    store.complete_work([again.id])
    clock.advance(hours=1)
    (retried,) = store.claim_work("blob", "b", limit=5, until=_later(minutes=5))
    assert (retried.key, retried.last_error, retried.attempts) == (failed.key, "boom", 2)


def test_a_claim_that_is_renewed_is_not_claimed_again_until_the_renewals_stop(
    store: SQLStoreBase[Any], clock: Clock
) -> None:
    _queue(store, 1)
    store.claim_work("blob", "a", limit=1, until=_later(minutes=5))
    for _ in range(3):
        clock.advance(minutes=4)
        store.renew_work("a", until=_later(minutes=5))
        assert store.claim_work("blob", "b", limit=1, until=_later(minutes=5)) == []
    clock.advance(minutes=6)
    store.renew_work("a", until=_later(minutes=5))  # too late: it ran out, so it is b's to claim
    assert [w.claimed_by for w in store.claim_work("blob", "b", limit=1, until=_later(minutes=5))] == ["b"]


@pytest.mark.postgres
@pytest.mark.parametrize("store_backend", [StoreBackend.POSTGRES], indirect=True)
def test_a_claim_skips_the_items_another_is_claiming_instead_of_waiting_for_them(
    store: SQLStoreBase[Any],
) -> None:
    """Postgres's `SKIP LOCKED`: a claimer holding rows in an open transaction slows no other claimer down."""
    _queue(store, 4)
    work = store.tables[WorkItem]
    with store._engine.connect() as held, held.begin():
        locked = (
            held.execute(sa.select(work.c.key).order_by(work.c.id).limit(2).with_for_update()).scalars().all()
        )
        claimed: list[WorkItem] = []
        claimer = threading.Thread(
            target=lambda: claimed.extend(store.claim_work("blob", "b", limit=4, until=_later(minutes=5)))
        )
        claimer.start()
        claimer.join(timeout=5)
        assert not claimer.is_alive(), "the claim waited on the rows another transaction holds"
    assert sorted(w.key for w in claimed) == sorted({"0", "1", "2", "3"} - set(locked))
