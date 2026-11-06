"""Kept runs and the run list's filters a retention rule reads, on every backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pendulum
import pytest

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend, create_entity_chain

if TYPE_CHECKING:
    from dlboard.serve import SQLStoreBase


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


def test_a_kept_run_stays_kept_by_whoever_kept_it_until_it_is_released(
    store: SQLStoreBase[Any], experiment_id: int, admin: models.User
) -> None:
    alice = store.get_or_create_user(models.Principal.unverified("alice"))
    a, b, gone = (store.create_run(models.NewRun(experiment_id=experiment_id)).id for _ in range(3))
    store.keep_runs(frozenset({a}), alice)
    store.keep_runs(frozenset({a, b}), admin)
    assert store.kept_runs(experiment_id, run_ids=None) == {a, b}
    assert store.kept_runs(experiment_id, run_ids=frozenset({b, gone})) == {b}
    kept_a = store.get_run(a)
    assert kept_a is not None
    assert kept_a.kept_by == alice.id
    store.keep_runs(frozenset({gone}), alice)
    store.delete_run(gone, admin)
    assert store.kept_runs(experiment_id, run_ids=None) == {a, b}
    with pytest.raises(ValueError, match="has been deleted"):
        store.keep_runs(frozenset({gone}), alice)
    store.release_runs(frozenset({a}), admin)
    assert store.kept_runs(experiment_id, run_ids=None) == {b}


def test_a_run_list_reads_from_either_end_older_than_a_time_and_without_the_kept_ones(
    store: SQLStoreBase[Any], experiment_id: int, admin: models.User
) -> None:
    start = pendulum.now("UTC").subtract(days=10)
    runs = [
        store.create_run(models.NewRun(experiment_id=experiment_id, created_at=start.add(days=i))).id
        for i in range(5)
    ]
    store.keep_runs(frozenset({runs[1]}), admin)

    def ids(**filters: Any) -> list[int]:  # noqa: ANN401
        return [r.id for r in store.get_runs(experiment_id, run_ids=None, **filters)]

    assert ids() == runs[::-1]
    assert ids(oldest_first=True, limit=2) == runs[:2]
    assert ids(created_before=start.add(days=3), oldest_first=True) == runs[:3]
    assert ids(include_kept=False, oldest_first=True) == [runs[0], *runs[2:]]


def test_a_delete_that_spares_kept_runs_leaves_one_kept_after_it_was_chosen_and_what_it_owns(
    store: SQLStoreBase[Any], admin: models.User
) -> None:
    chain = create_entity_chain(store, artifact=True)
    other = store.create_run(models.NewRun(experiment_id=chain.experiment_id)).id
    chosen = frozenset({chain.run_id, other})
    store.keep_runs(frozenset({chain.run_id}), admin)  # kept after the caller chose: nothing re-reads it

    assert store.delete_runs(chosen, admin, spare_kept=True) == {other}
    assert [r.id for r in store.get_runs(chain.experiment_id, run_ids=None)] == [chain.run_id]
    assert list(store.list_deleted_artifacts()) == []

    def audited() -> set[int]:
        return {
            e.entity_id
            for e in store.list_audit_log(admin, limit=1_000)
            if e.action is models.AuditAction.SOFT_DELETE and e.entity_type is models.EntityType.RUN
        }

    assert audited() == {other}  # a run that was spared was not deleted, so the log does not say it was
    assert store.delete_runs(chosen - {other}, admin, spare_kept=False) == {chain.run_id}
    assert audited() == chosen


@pytest.mark.parametrize("include_kept", [True, False], ids=["with the kept", "without them"])
def test_a_count_of_runs_agrees_with_the_list_it_stands_for_and_stops_at_its_limit(
    store: SQLStoreBase[Any], experiment_id: int, admin: models.User, *, include_kept: bool
) -> None:
    now = pendulum.now("UTC")
    same_second = now.subtract(days=5)
    runs = [
        store.create_run(models.NewRun(experiment_id=experiment_id, created_at=when, status=status)).id
        for when, status in (
            (now.subtract(days=9), models.RunStatus.FINISHED),
            (same_second, models.RunStatus.FINISHED),
            (same_second, models.RunStatus.FINISHED),  # created at once: told apart by id
            (now.subtract(days=6), models.RunStatus.RUNNING),  # running, and within its grace
            (now.subtract(days=8), models.RunStatus.RUNNING),  # running, and past it
        )
    ]
    store.keep_runs(frozenset({runs[0]}), admin)
    grace = now.subtract(days=7)
    cut = (same_second, runs[2])
    # Before the cut: the kept run, the one created at once with it (by id), and the abandoned one; not the one still running.
    counted = store.count_runs(
        experiment_id, before=cut, include_kept=include_kept, spare_running_after=grace, limit=100
    )
    assert counted == (3 if include_kept else 2)
    assert store.count_runs(experiment_id, limit=100) == len(runs)
    assert store.count_runs(experiment_id, limit=2) == 2
