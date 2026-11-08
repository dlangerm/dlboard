"""Purging a very large run goes in paced chunks, and loses neither a row nor a blob on the way (#563)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pendulum
import pytest
import sqlalchemy as sa

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend, create_entity_chain, recorded_statements
from dlboard.serve._backend import _sql_store_base as store_base
from dlboard.serve._backend._purge_work import PurgeWork

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from typing import Any

    from pydantic import BaseModel

    from dlboard.serve import SQLStoreBase

_STEPS = 8
"""A run of two metrics a step and an artifact a step: sixteen metric rows and eight artifacts, in chunks of three."""


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


@pytest.fixture(autouse=True)
def _small_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_base, "_PURGE_CHUNK", 3)
    monkeypatch.setattr(store_base, "_PURGE_ARTIFACT_CHUNK", 3)


class _Pace:
    """A purge's pace that counts the chunks it is entered for, and may do something as each is entered."""

    def __init__(self, on_enter: Callable[[int], None] = lambda _: None) -> None:
        self.entered = 0
        self._on_enter = on_enter

    @contextmanager
    def __call__(self) -> Generator[None]:
        self.entered += 1
        self._on_enter(self.entered)
        yield


def _trashed_large_run(store: SQLStoreBase[Any], admin: models.User) -> tuple[int, int]:
    """An experiment and its run, with `_STEPS` of metrics and artifacts logged, in the Trash."""
    chain = create_entity_chain(store)
    now = pendulum.now(pendulum.UTC)
    store.log_metrics(
        models.LoggedMetrics(
            metrics={"loss": 1.0, "acc": 0.5},
            step=step,
            experiment_id=chain.experiment_id,
            run_id=chain.run_id,
            timestamp_utc=now,
        )
        for step in range(_STEPS)
    )
    store.log_artifact_refs(
        models.Artifact(
            key="img",
            fname="f.png",
            run_id=chain.run_id,
            experiment_id=chain.experiment_id,
            step=step,
            ref=f"ref://{chain.run_id}/{step}",
        )
        for step in range(_STEPS)
    )
    store.delete_run(chain.run_id, admin)
    return chain.experiment_id, chain.run_id


def _rows(store: SQLStoreBase[Any], model: type[BaseModel]) -> int:
    table = store._tables[model]
    return store._execute(sa.select(sa.func.count()).select_from(table))[0][0]


def test_a_large_run_is_purged_in_paced_chunks_and_nothing_is_lost_or_forgotten(
    store: SQLStoreBase[Any], admin: models.User
) -> None:
    _, run_id = _trashed_large_run(store, admin)
    pace = _Pace()

    store.purge_run(run_id, admin, pace=pace)

    assert _rows(store, models.UnderlyingMetricTableEntry) == 0
    assert _rows(store, models.Artifact) == 0
    assert store.count_work(PurgeWork.BLOB) == _STEPS  # every artifact's blob is queued, once
    assert (
        pace.entered >= 9
    )  # six chunks of metrics and three of artifacts, at the least: each one rested after
    (entry,) = [e for e in store.list_audit_log(admin) if e.action is models.AuditAction.PURGE]
    assert json.loads(entry.details)["UnderlyingMetricTableEntry"] == 2 * _STEPS
    assert json.loads(entry.details)["Artifact"] == _STEPS


def test_a_purge_that_stops_between_chunks_goes_on_later_without_losing_or_repeating_a_blob(
    store: SQLStoreBase[Any], admin: models.User
) -> None:
    _, run_id = _trashed_large_run(store, admin)

    def stop_at_the_fourth(entered: int) -> None:
        if entered == 4:
            raise InterruptedError

    with pytest.raises(InterruptedError):
        store.purge_run(run_id, admin, pace=_Pace(stop_at_the_fourth))
    assert (
        0 < _rows(store, models.UnderlyingMetricTableEntry) < 2 * _STEPS
    )  # part-way, the run still in the Trash

    store.purge_run(run_id, admin)

    assert _rows(store, models.UnderlyingMetricTableEntry) == 0
    assert _rows(store, models.Artifact) == 0
    assert store.count_work(PurgeWork.BLOB) == _STEPS


def test_a_run_restored_while_its_purge_goes_on_is_not_purged(
    store: SQLStoreBase[Any], admin: models.User
) -> None:
    """What was deleted before it was restored is gone; nothing after is, nor is the run."""

    def restore_at_the_second(entered: int) -> None:
        if entered == 2:
            store.restore_run(run_id, admin)

    _, run_id = _trashed_large_run(store, admin)

    store.purge_run(run_id, admin, pace=_Pace(restore_at_the_second))

    assert store.get_run(run_id) is not None
    assert (
        _rows(store, models.UnderlyingMetricTableEntry) == 2 * _STEPS - 3
    )  # the one chunk before the restore
    assert _rows(store, models.Artifact) == _STEPS
    assert store.count_work(PurgeWork.BLOB) == 0


def test_a_chunk_of_artifacts_deletes_before_it_queues_so_that_it_can_wait_for_a_lock(
    store: SQLStoreBase[Any], admin: models.User
) -> None:
    """
    A chunk's transaction starts with its write. SQLite fails a read that is followed by a write at once ("database is
    locked") if another writer got in between, where a write that comes first waits for the lock: found by running a purge
    beside a writer, which no test without one sees.
    """
    _, run_id = _trashed_large_run(store, admin)

    with recorded_statements(store._engine) as statements:
        store.purge_run(run_id, admin, pace=_Pace())

    queued = [i for i, s in enumerate(statements) if s.sql.startswith("INSERT") and "WorkItem" in s.sql]
    assert len(queued) == 3
    assert all(
        statements[i - 1].sql.startswith("DELETE") and '"Artifact"' in statements[i - 1].sql for i in queued
    )
