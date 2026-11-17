"""Writes that race on every backend: what was decided by one must not be undone, or done twice, by another."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend
from dlboard.serve._backend._authorization import AuthorizingDataStore

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


def test_one_persons_jobs_starting_at_once_with_a_new_project_name_get_one_project(
    store: SQLLiteStore,
) -> None:
    """Regression (#666): each found no project of the name it could write to, and each created one."""
    alice = store.get_or_create_user(models.Principal.unverified("alice"))

    def start(_job: int) -> int:
        # A store per request, as every request gets one.
        return AuthorizingDataStore(store, alice, enforce_grants=True).get_or_create_project("mnist").id

    with ThreadPoolExecutor(max_workers=_RACERS) as pool:
        got = list(pool.map(start, range(_RACERS)))

    assert len(set(got)) == 1
    assert AuthorizingDataStore(store, alice, enforce_grants=True).get_or_create_project("mnist").id == got[0]


def test_a_password_is_set_once_by_whoever_gets_there_first(store: SQLLiteStore) -> None:
    """What signing up rests on: of two signups for one new name, the second never replaces the first's password."""
    user = store.get_or_create_user(models.Principal.unverified("erin"))

    first = store.add_password_credential(models.PasswordCredential(user_id=user.id, password_hash="first"))
    second = store.add_password_credential(models.PasswordCredential(user_id=user.id, password_hash="second"))

    assert (first, second) == (True, False)
    credential = store.get_password_credential(user.id)
    assert credential is not None
    assert credential.password_hash == "first"
