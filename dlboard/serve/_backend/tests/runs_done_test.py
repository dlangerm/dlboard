"""`Run.done_at` and `runs_done_since`: every run that ends is found once, from wherever a reader left off (#704)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pendulum
import pytest

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend

if TYPE_CHECKING:
    from dlboard.serve._backend._sql_store_base import SQLStoreBase


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


_LATER = pendulum.now("UTC").add(years=1)
"""A `before` no run of a test reaches."""


def _page_through(store: SQLStoreBase[Any], *, limit: int) -> list[int]:
    found: list[int] = []
    after = None
    while page := store.runs_done_since(after, before=_LATER, limit=limit):
        found += [run.id for run in page]
        last = page[-1]
        assert last.done_at is not None
        after = (last.done_at, last.id)
    return found


def test_every_run_that_ended_is_read_once_from_wherever_the_last_page_stopped(
    store: SQLStoreBase[Any], experiment_id: int
) -> None:
    """A page at a time, in the order they ended: not a running run, not a deleted one, and one reported twice once."""
    runs = [store.create_run(models.NewRun(experiment_id=experiment_id)).id for _ in range(7)]
    long_ago = pendulum.datetime(2020, 1, 1, tz="UTC")
    # Their own end, long past, is not the order: when the server heard it is.
    for run_id in reversed(runs[:5]):
        store.finish_run(run_id, models.RunStatus.FINISHED, long_ago)
    store.finish_run(runs[4], models.RunStatus.FINISHED, long_ago)  # Lightning finalizing again
    store.delete_run(runs[3], store.get_or_create_user(models.Principal.unverified("me")))

    assert _page_through(store, limit=2) == [runs[4], runs[2], runs[1], runs[0]]


def test_a_run_that_ends_another_way_is_read_again_after_the_rest(
    store: SQLStoreBase[Any], experiment_id: int
) -> None:
    first, second = (store.create_run(models.NewRun(experiment_id=experiment_id)).id for _ in range(2))
    store.finish_run(first, models.RunStatus.FINISHED, pendulum.now("UTC"))
    store.finish_run(second, models.RunStatus.FINISHED, pendulum.now("UTC"))
    store.finish_run(first, models.RunStatus.FAILED, pendulum.now("UTC"))

    assert _page_through(store, limit=10) == [second, first]


def test_a_lost_run_ended_when_it_was_given_up_and_runs_again_when_it_is_heard_from(
    store: SQLStoreBase[Any], experiment_id: int
) -> None:
    """Given up now, though it ended (was last heard from) long before: a reader past then still finds it."""
    run_id = store.create_run(models.NewRun(experiment_id=experiment_id)).id
    before = pendulum.now("UTC")
    store.mark_lost_runs(pendulum.now("UTC").add(seconds=1), limit=10)

    (lost,) = store.runs_done_since(None, before=_LATER, limit=10)
    assert lost.id == run_id
    assert lost.done_at is not None
    assert lost.done_at >= before

    store.heartbeat(run_id)
    assert store.runs_done_since(None, before=_LATER, limit=10) == []


def test_a_run_that_ended_at_or_after_before_is_left_for_a_later_read(
    store: SQLStoreBase[Any], experiment_id: int
) -> None:
    run_id = store.create_run(models.NewRun(experiment_id=experiment_id)).id
    earlier = pendulum.now("UTC")
    store.finish_run(run_id, models.RunStatus.FINISHED, pendulum.now("UTC"))
    assert store.runs_done_since(None, before=earlier, limit=10) == []
    assert [r.id for r in store.runs_done_since(None, before=_LATER, limit=10)] == [run_id]
