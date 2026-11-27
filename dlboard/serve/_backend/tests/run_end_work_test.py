"""
A run's end is queued for run-end work (`queue_on_run_end`) in the transaction that records it (#704): each new end once
for every kind asked for, a repeated one not again, and none if the end itself isn't written.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pendulum
import pytest

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend
from dlboard.serve import RunEnded

if TYPE_CHECKING:
    from dlboard.serve._backend._sql_store_base import SQLStoreBase

_KINDS = frozenset({"notify", "summarize"})


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


@pytest.fixture
def asked(store: SQLStoreBase[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Two plugins' kinds of run-end work, on the shared store for this test alone."""
    monkeypatch.setattr(store, "_run_end_work", _KINDS)


def _queued(store: SQLStoreBase[Any], kind: str) -> list[RunEnded]:
    claimed = store.claim_work(kind, "test", limit=100, until=pendulum.now("UTC").add(minutes=1))
    store.complete_work([item.id for item in claimed])
    return [RunEnded.model_validate(item.payload) for item in claimed]


@pytest.mark.usefixtures("asked")
def test_each_new_end_is_queued_once_for_every_kind(store: SQLStoreBase[Any], experiment_id: int) -> None:
    """Not a repeated end (Lightning finalizing after `fit` and `test`), not a lost run heard from again, not a running one."""
    finished, lost = (store.create_run(models.NewRun(experiment_id=experiment_id)).id for _ in range(2))
    store.finish_run(finished, models.RunStatus.FINISHED, pendulum.now("UTC"))
    store.finish_run(finished, models.RunStatus.FINISHED, pendulum.now("UTC"))
    store.mark_lost_runs(pendulum.now("UTC").add(seconds=1), limit=10)
    store.heartbeat(lost)  # running again: its end stays queued, for the work to see it no longer is
    store.create_run(models.NewRun(experiment_id=experiment_id))  # running, and never ends
    store.finish_run(finished, models.RunStatus.FAILED, pendulum.now("UTC"))  # ended another way: a new end

    for kind in _KINDS:
        ends = _queued(store, kind)
        assert sorted(end.run_id for end in ends) == sorted([finished, finished, lost])
        assert len({end.key for end in ends}) == len(ends)
        latest = max((end for end in ends if end.run_id == finished), key=lambda end: end.done_at)
        run = store.get_run(finished)
        assert run is not None
        assert latest.done_at == run.done_at


def test_no_end_is_queued_when_no_plugin_asks(store: SQLStoreBase[Any], experiment_id: int) -> None:
    run_id = store.create_run(models.NewRun(experiment_id=experiment_id)).id
    store.finish_run(run_id, models.RunStatus.FINISHED, pendulum.now("UTC"))

    assert all(store.count_work(kind) == 0 for kind in _KINDS)


@pytest.mark.usefixtures("asked")
def test_an_end_that_cannot_be_queued_is_not_recorded_either(
    store: SQLStoreBase[Any], experiment_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All or nothing: no end is recorded that run-end work never hears of."""
    run_id = store.create_run(models.NewRun(experiment_id=experiment_id)).id

    def broken(*_args: object) -> None:
        msg = "the queue is full"
        raise RuntimeError(msg)

    monkeypatch.setattr(store, "_queue_run_ends", broken)
    with pytest.raises(RuntimeError, match="the queue is full"):
        store.finish_run(run_id, models.RunStatus.FINISHED, pendulum.now("UTC"))

    run = store.get_run(run_id)
    assert run is not None
    assert (run.status, run.done_at) == (models.RunStatus.RUNNING, None)
