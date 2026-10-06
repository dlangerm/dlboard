from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum

from dlboard import models

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


def _experiment(store: SQLLiteStore, project_id: int, runs: int) -> int:
    experiment = store.create_experiment(models.NewExperiment(project_id=project_id))
    for _ in range(runs):
        store.create_run(models.NewRun(experiment_id=experiment.id))
    return experiment.id


def test_stats_count_only_what_is_not_deleted(store: SQLLiteStore, admin: models.User) -> None:
    project = store.create_project(models.NewProject(name="p", description=""))
    kept = _experiment(store, project.id, runs=3)
    deleted = _experiment(store, project.id, runs=2)
    store.delete_experiment(deleted, admin)
    store.delete_run(next(store.get_runs(kept)).id, admin)
    empty = store.create_project(models.NewProject(name="empty", description=""))
    gone = store.create_project(models.NewProject(name="gone", description=""))
    _experiment(store, gone.id, runs=1)
    store.delete_project(gone.id, admin)

    project_stats = store.get_project_stats()
    experiment_stats = store.get_experiment_stats(project.id)

    assert project_stats[project.id].experiment_count == 1
    assert project_stats[project.id].run_count == 2
    assert empty.id not in project_stats
    assert gone.id not in project_stats
    assert experiment_stats.keys() == {kept}
    assert experiment_stats[kept].run_count == 2


def test_last_activity_is_the_server_time_of_the_latest_write(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description=""))
    idle = _experiment(store, project.id, runs=0)
    before = pendulum.now("UTC")
    active = _experiment(store, project.id, runs=1)

    experiment_stats = store.get_experiment_stats(project.id)
    project_stats = store.get_project_stats()[project.id]

    assert experiment_stats[idle].last_activity_at is None
    last_activity = experiment_stats[active].last_activity_at
    assert last_activity is not None
    assert last_activity >= before
    assert project_stats.last_activity_at == last_activity


def test_experiment_stats_are_scoped_to_their_project(store: SQLLiteStore) -> None:
    mine = store.create_project(models.NewProject(name="mine", description=""))
    theirs = store.create_project(models.NewProject(name="theirs", description=""))
    my_experiment = _experiment(store, mine.id, runs=1)
    _experiment(store, theirs.id, runs=4)

    assert store.get_experiment_stats(mine.id).keys() == {my_experiment}
