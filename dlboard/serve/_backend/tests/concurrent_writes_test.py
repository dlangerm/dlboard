"""Writes that race on every backend: what was decided by one must not be undone, or done twice, by another."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

_RACERS = 8


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


def test_a_sweep_starting_at_once_gets_one_experiment(store: SQLLiteStore) -> None:
    """Regression: each worker found no experiment of the name, and each created one."""
    project = store.create_project(models.NewProject(name="p", description=""))

    def start(_worker: int) -> int:
        return store.get_or_create_experiment(project.id, "sweep").id

    with ThreadPoolExecutor(max_workers=_RACERS) as pool:
        got = list(pool.map(start, range(_RACERS)))

    assert len(set(got)) == 1
    assert [e.id for e in store.get_experiments(project.id)] == got[:1]


def test_a_run_keeps_the_first_hyperparameters_logged_to_it(store: SQLLiteStore, experiment_id: int) -> None:
    """Regression: processes sharing a run both stored theirs, and a second call was dropped without a word."""
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    sent = [models.NewHyperParams.from_raw(run.id, experiment_id, {"lr": float(i)}) for i in range(_RACERS)]

    def log(hparams: models.NewHyperParams) -> models.FlatHparamDict:
        return store.log_hyperparams(hparams).hparams_dict

    with ThreadPoolExecutor(max_workers=_RACERS) as pool:
        kept = list(pool.map(log, sent))

    table = store._tables[models.HyperParams]
    ((rows,),) = store._execute(sa.select(sa.func.count()).select_from(table).where(table.c.run_id == run.id))
    assert rows == 1
    # Every caller is told what was kept, to warn when it wasn't its own.
    assert len({str(h) for h in kept}) == 1


def test_an_edit_read_before_a_delete_does_not_bring_the_project_back(
    store: SQLLiteStore, admin: models.User
) -> None:
    """Regression: an update wrote back every column of the copy it was given, `deleted_at` included."""
    project = store.create_project(models.NewProject(name="p", description="d"))
    store.delete_project(project.id, admin)

    store.update_project(project.model_copy(update={"description": "edited"}))

    assert store.get_project(project.id) is None
