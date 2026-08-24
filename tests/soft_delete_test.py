# pyright: reportPrivateUsage=false
"""Tests for cascading soft-delete/restore/purge and the read/write guards around them."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum
import pytest

from dltrack import models
from dltrack.serve._backend._scope_enforcement import ScopeEnforcingDataStore

if TYPE_CHECKING:
    from dltrack.plugins.data_stores.sqlite import SQLLiteStore


@pytest.fixture
def admin(store: SQLLiteStore) -> models.User:
    """The bootstrap admin -- the first user any fresh store creates gets `Scope.ALL`."""
    return store.get_or_create_user("admin")


def _project_experiment_run_artifact(store: SQLLiteStore) -> tuple[int, int, int, int]:
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    run = store.create_run(models.NewRun(experiment_id=experiment.id))
    store.log_artifact_refs(
        [
            models.Artifact(
                key="img", fname="i.png", run_id=run.id, experiment_id=experiment.id, step=0, ref="ref://a"
            )
        ]
    )
    (artifact,) = list(store.fetch_artifacts(experiment_id=experiment.id))
    assert artifact.id is not None
    return project.id, experiment.id, run.id, artifact.id


def test_delete_project_cascades_to_experiments_runs_and_artifacts(
    store: SQLLiteStore, admin: models.User
) -> None:
    project_id, experiment_id, _run_id, _artifact_id = _project_experiment_run_artifact(store)

    store.delete_project(project_id, admin)

    assert project_id not in {p.id for p in store.get_projects()}
    assert list(store.get_experiments(project_id)) == []
    assert list(store.fetch_artifacts(experiment_id=experiment_id)) == []
    assert experiment_id in {e.id for e in store.list_deleted_experiments()}


def test_delete_project_twice_is_a_noop_error(store: SQLLiteStore, admin: models.User) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    store.delete_project(project.id, admin)

    with pytest.raises(ValueError, match="already deleted"):
        store.delete_project(project.id, admin)


def test_delete_project_on_unknown_id_raises(store: SQLLiteStore, admin: models.User) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        store.delete_project(999_999, admin)


def test_restore_project_restores_cascaded_children(store: SQLLiteStore, admin: models.User) -> None:
    project_id, experiment_id, _run_id, artifact_id = _project_experiment_run_artifact(store)
    store.delete_project(project_id, admin)

    store.restore_project(project_id, admin)

    assert project_id in {p.id for p in store.get_projects()}
    assert experiment_id in {e.id for e in store.get_experiments(project_id)}
    assert artifact_id in {a.id for a in store.fetch_artifacts(experiment_id=experiment_id)}


def test_restore_project_does_not_resurrect_an_independently_deleted_experiment(
    store: SQLLiteStore, admin: models.User
) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    kept = store.create_experiment(models.NewExperiment(project_id=project.id, name="kept"))
    independently_deleted = store.create_experiment(
        models.NewExperiment(project_id=project.id, name="already-gone")
    )

    store.delete_experiment(independently_deleted.id, admin)
    store.delete_project(project.id, admin)
    store.restore_project(project.id, admin)

    experiment_ids = {e.id for e in store.get_experiments(project.id)}
    assert kept.id in experiment_ids
    assert independently_deleted.id not in experiment_ids, (
        "an experiment deleted before the project must not be resurrected by restoring the project"
    )


def test_restore_requires_the_entity_to_be_deleted(store: SQLLiteStore, admin: models.User) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))

    with pytest.raises(ValueError, match="not deleted"):
        store.restore_project(project.id, admin)


def test_delete_experiment_cascades_to_runs_and_artifacts(store: SQLLiteStore, admin: models.User) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    run = store.create_run(models.NewRun(experiment_id=experiment.id))

    store.delete_experiment(experiment.id, admin)

    assert store.get_experiment(experiment.id) is None
    assert run.id in {r.id for r in store.list_deleted_runs()}
    # the project itself is untouched -- only the experiment and its descendants were deleted
    assert project.id in {p.id for p in store.get_projects()}


def test_delete_run_cascades_to_artifacts_only(store: SQLLiteStore, admin: models.User) -> None:
    project_id, experiment_id, run_id, artifact_id = _project_experiment_run_artifact(store)

    store.delete_run(run_id, admin)

    assert artifact_id in {a.id for a in store.list_deleted_artifacts()}
    # experiment and project survive a run-level delete
    assert experiment_id in {e.id for e in store.get_experiments(project_id)}


def test_delete_artifact_has_no_cascade(store: SQLLiteStore, admin: models.User) -> None:
    _project_id, experiment_id, run_id, artifact_id = _project_experiment_run_artifact(store)

    store.delete_artifact(artifact_id, admin)

    assert list(store.fetch_artifacts(experiment_id=experiment_id)) == []
    # the run it belonged to is untouched
    assert store._fetch_deleted_at(models.Run, run_id) is None


def test_purge_requires_scope(store: SQLLiteStore, admin: models.User) -> None:
    """Scope enforcement lives in `ScopeEnforcingDataStore` (see `_scope_enforcement.py`), not in
    `SQLStoreBase` -- so this goes through the wrapper, exactly like every store does once it's
    registered with a running app via `set_data_store`."""
    project = store.create_project(models.NewProject(name="p", description="d"))
    store.delete_project(project.id, admin)
    no_scopes_user = store.get_or_create_user("nobody")
    wrapped = ScopeEnforcingDataStore(store)

    with pytest.raises(PermissionError, match="lacks"):
        wrapped.purge_project(project.id, no_scopes_user)


@pytest.mark.parametrize(
    ("method", "scope"),
    [
        ("delete_project", models.Scope.PROJECT_DELETE),
        ("delete_experiment", models.Scope.EXPERIMENT_DELETE),
        ("delete_run", models.Scope.RUN_DELETE),
        ("delete_artifact", models.Scope.ARTIFACT_DELETE),
    ],
)
def test_delete_requires_the_matching_scope(
    store: SQLLiteStore, admin: models.User, method: str, scope: models.Scope
) -> None:
    """Goes through `ScopeEnforcingDataStore` -- see `test_purge_requires_scope`."""
    project_id, experiment_id, run_id, artifact_id = _project_experiment_run_artifact(store)
    entity_id = {
        "delete_project": project_id,
        "delete_experiment": experiment_id,
        "delete_run": run_id,
        "delete_artifact": artifact_id,
    }[method]
    no_scopes_user = store.get_or_create_user("nobody")
    wrapped = ScopeEnforcingDataStore(store)

    with pytest.raises(PermissionError, match="lacks"):
        getattr(wrapped, method)(entity_id, no_scopes_user)

    scoped_user = store.get_or_create_user("scoped")
    store.update_user(scoped_user.model_copy(update={"scopes": [scope]}))
    getattr(wrapped, method)(entity_id, store.get_or_create_user("scoped"))  # does not raise


def test_restore_requires_the_restore_scope(store: SQLLiteStore, admin: models.User) -> None:
    """Goes through `ScopeEnforcingDataStore` -- see `test_purge_requires_scope`."""
    project = store.create_project(models.NewProject(name="p", description="d"))
    store.delete_project(project.id, admin)
    no_scopes_user = store.get_or_create_user("nobody")
    wrapped = ScopeEnforcingDataStore(store)

    with pytest.raises(PermissionError, match="lacks"):
        wrapped.restore_project(project.id, no_scopes_user)

    scoped_user = store.get_or_create_user("scoped")
    store.update_user(scoped_user.model_copy(update={"scopes": [models.Scope.RESTORE]}))
    wrapped.restore_project(project.id, store.get_or_create_user("scoped"))  # does not raise


def test_purge_requires_prior_soft_delete(store: SQLLiteStore, admin: models.User) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))

    with pytest.raises(ValueError, match="must be soft-deleted"):
        store.purge_project(project.id, admin)


def test_purge_project_permanently_removes_everything_under_it(
    store: SQLLiteStore, admin: models.User
) -> None:
    project_id, experiment_id, run_id, artifact_id = _project_experiment_run_artifact(store)
    store.delete_project(project_id, admin)

    store.purge_project(project_id, admin)

    assert project_id not in {p.id for p in store.list_deleted_projects()}
    assert experiment_id not in {e.id for e in store.list_deleted_experiments()}
    assert run_id not in {r.id for r in store.list_deleted_runs()}
    assert artifact_id not in {a.id for a in store.list_deleted_artifacts()}
    with pytest.raises(ValueError, match="does not exist"):
        store.restore_project(project_id, admin)


def test_purge_project_queues_an_artifact_purge_task_for_each_cascaded_artifact(
    store: SQLLiteStore, admin: models.User
) -> None:
    project_id, _experiment_id, _run_id, artifact_id = _project_experiment_run_artifact(store)
    store.delete_project(project_id, admin)

    store.purge_project(project_id, admin)

    (task,) = list(store.list_pending_artifact_purges())
    assert task.artifact_id == artifact_id
    assert task.ref == "ref://a"
    assert task.requested_by == admin.id
    assert store.count_pending_artifact_purges() == 1


def test_purge_artifact_directly_queues_its_own_purge_task(store: SQLLiteStore, admin: models.User) -> None:
    _project_id, _experiment_id, _run_id, artifact_id = _project_experiment_run_artifact(store)
    store.delete_artifact(artifact_id, admin)

    store.purge_artifact(artifact_id, admin)

    (task,) = list(store.list_pending_artifact_purges())
    assert task.artifact_id == artifact_id


def test_purge_with_no_artifacts_queues_nothing(store: SQLLiteStore, admin: models.User) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    store.delete_project(project.id, admin)

    store.purge_project(project.id, admin)

    assert store.count_pending_artifact_purges() == 0


def test_complete_artifact_purge_deletes_the_task_row(store: SQLLiteStore, admin: models.User) -> None:
    project_id, *_rest = _project_experiment_run_artifact(store)
    store.delete_project(project_id, admin)
    store.purge_project(project_id, admin)
    (task,) = list(store.list_pending_artifact_purges())
    assert task.id is not None

    store.complete_artifact_purge(task.id)

    assert store.count_pending_artifact_purges() == 0


def test_fail_artifact_purge_records_the_error_and_keeps_the_task_pending(
    store: SQLLiteStore, admin: models.User
) -> None:
    project_id, *_rest = _project_experiment_run_artifact(store)
    store.delete_project(project_id, admin)
    store.purge_project(project_id, admin)
    (task,) = list(store.list_pending_artifact_purges())
    assert task.id is not None

    store.fail_artifact_purge(task.id, "disk is on fire")

    (retried,) = list(store.list_pending_artifact_purges())
    assert retried.last_error == "disk is on fire"


def test_create_experiment_rejected_for_a_deleted_project(store: SQLLiteStore, admin: models.User) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    store.delete_project(project.id, admin)

    with pytest.raises(ValueError, match="has been deleted"):
        store.create_experiment(models.NewExperiment(project_id=project.id))


def test_create_run_rejected_for_a_deleted_experiment(
    store: SQLLiteStore, experiment_id: int, admin: models.User
) -> None:
    store.delete_experiment(experiment_id, admin)

    with pytest.raises(ValueError, match="has been deleted"):
        store.create_run(models.NewRun(experiment_id=experiment_id))


def test_log_metrics_rejected_against_a_deleted_run(
    store: SQLLiteStore, experiment_id: int, admin: models.User
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.delete_run(run.id, admin)

    with pytest.raises(ValueError, match="has been deleted"):
        store.log_metrics(
            [
                models.LoggedMetrics(
                    metrics={"loss": 0.1},
                    step=0,
                    experiment_id=experiment_id,
                    run_id=run.id,
                    timestamp_utc=pendulum.now(pendulum.UTC),
                )
            ]
        )


def test_log_hyperparams_rejected_against_a_deleted_run(
    store: SQLLiteStore, experiment_id: int, admin: models.User
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.delete_run(run.id, admin)

    with pytest.raises(ValueError, match="has been deleted"):
        store.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment_id, {"lr": 0.1}))


def test_log_artifact_refs_rejected_against_a_deleted_run(
    store: SQLLiteStore, experiment_id: int, admin: models.User
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.delete_run(run.id, admin)

    with pytest.raises(ValueError, match="has been deleted"):
        store.log_artifact_refs(
            [
                models.Artifact(
                    key="img",
                    fname="i.png",
                    run_id=run.id,
                    experiment_id=experiment_id,
                    step=0,
                    ref="ref://a",
                )
            ]
        )


def test_fetch_metrics_excludes_metrics_from_a_deleted_run(
    store: SQLLiteStore, experiment_id: int, admin: models.User
) -> None:
    kept_run = store.create_run(models.NewRun(experiment_id=experiment_id))
    deleted_run = store.create_run(models.NewRun(experiment_id=experiment_id))
    now = pendulum.now(pendulum.UTC)
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"loss": 0.1},
                step=0,
                experiment_id=experiment_id,
                run_id=kept_run.id,
                timestamp_utc=now,
            ),
            models.LoggedMetrics(
                metrics={"loss": 0.2},
                step=0,
                experiment_id=experiment_id,
                run_id=deleted_run.id,
                timestamp_utc=now,
            ),
        ]
    )

    store.delete_run(deleted_run.id, admin)

    fetched = list(store.fetch_metrics(experiment_id))
    assert {m.run_id for m in fetched} == {kept_run.id}
