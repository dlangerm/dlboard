"""A run whose process died stops reading running: given up as lost once quiet long enough, running again if heard from."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pendulum
import pytest

from dlboard import models
from dlboard.conftest import dispose_stores, run_task
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dlboard.plugins.backend import lost_runs
from dlboard.plugins.backend.lost_runs import LostRunTask
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.serve import SQLStoreBase, get_system_data_store
from dlboard.serve import app as build_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from dash import Dash

    from dlboard.conftest import Clock


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Iterator[Dash]:
    del clock
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    built = build_app([sqlite, filesystem, lost_runs, *LOCAL_AUTH, *BUILTIN_BACKEND])
    yield built
    dispose_stores(built)


@pytest.fixture
def store(app: Dash) -> SQLStoreBase[Any]:
    store = get_system_data_store(app)
    assert isinstance(store, SQLStoreBase)
    return store


def _experiment(store: SQLStoreBase[Any]) -> int:
    project = store.create_project(models.NewProject(name="p", description=""))
    return store.create_experiment(models.NewExperiment(project_id=project.id)).id


def _log(store: SQLStoreBase[Any], run: models.Run) -> None:
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"loss": 1.0},
                step=0,
                experiment_id=run.experiment_id,
                run_id=run.id,
                timestamp_utc=pendulum.now("UTC"),
            )
        ]
    )


def _run(store: SQLStoreBase[Any], run: models.Run) -> models.Run:
    found = store.get_run(run.id)
    assert found is not None
    return found


def test_a_run_not_heard_from_for_long_is_lost_and_ended_when_it_was_last_seen(
    app: Dash, store: SQLStoreBase[Any], clock: Clock
) -> None:
    experiment = _experiment(store)
    quiet, busy = (store.create_run(models.NewRun(experiment_id=experiment)) for _ in range(2))
    _log(store, quiet)
    last_seen = _run(store, quiet).last_seen_at
    clock.advance(minutes=8)
    _log(store, busy)
    clock.advance(minutes=3)

    run_task(app, LostRunTask.MARK)

    lost = _run(store, quiet)
    assert (lost.status, lost.ended_at, lost.lost_at) == (
        models.RunStatus.UNKNOWN,
        last_seen,
        pendulum.now("UTC"),
    )
    assert (_run(store, busy).status, _run(store, busy).lost_at) == (models.RunStatus.RUNNING, None)


def test_a_lost_run_heard_from_again_runs_again(app: Dash, store: SQLStoreBase[Any], clock: Clock) -> None:
    run = store.create_run(models.NewRun(experiment_id=_experiment(store)))
    _log(store, run)
    clock.advance(minutes=11)
    run_task(app, LostRunTask.MARK)

    store.heartbeat(run.id)

    again = _run(store, run)
    assert (again.status, again.ended_at, again.lost_at) == (models.RunStatus.RUNNING, None, None)


def test_a_run_never_heard_from_counts_from_its_start_and_an_ended_one_is_left_alone(
    app: Dash, store: SQLStoreBase[Any], clock: Clock
) -> None:
    experiment = _experiment(store)
    silent, done = (store.create_run(models.NewRun(experiment_id=experiment)) for _ in range(2))
    store.finish_run(done.id, models.RunStatus.FINISHED, pendulum.now("UTC"))
    clock.advance(minutes=11)

    run_task(app, LostRunTask.MARK)

    assert (_run(store, silent).status, _run(store, silent).ended_at) == (
        models.RunStatus.UNKNOWN,
        silent.created_at,
    )
    assert (_run(store, done).status, _run(store, done).lost_at) == (models.RunStatus.FINISHED, None)


def test_a_run_is_noted_as_heard_from_at_most_twice_a_minute(store: SQLStoreBase[Any], clock: Clock) -> None:
    """Its batches arrive every few seconds: a write of `Run` each would be a write per batch more."""
    run = store.create_run(models.NewRun(experiment_id=_experiment(store)))
    _log(store, run)
    first = _run(store, run).last_seen_at
    clock.advance(seconds=10)
    _log(store, run)
    unchanged = _run(store, run).last_seen_at
    clock.advance(seconds=25)
    _log(store, run)

    assert (unchanged, _run(store, run).last_seen_at) == (first, pendulum.now("UTC"))
