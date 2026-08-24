"""Tests for the audit log written by soft-delete/restore/purge."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from dltrack import models

if TYPE_CHECKING:
    from dltrack.plugins.data_stores.sqlite import SQLLiteStore


@pytest.fixture
def admin(store: SQLLiteStore) -> models.User:
    """The bootstrap admin -- the first user any fresh store creates gets `Scope.ALL`."""
    return store.get_or_create_user("admin")


def _project_with_one_experiment(store: SQLLiteStore) -> tuple[int, int]:
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    return project.id, experiment.id


def test_delete_project_records_a_soft_delete_audit_entry(store: SQLLiteStore, admin: models.User) -> None:
    project_id, _experiment_id = _project_with_one_experiment(store)

    store.delete_project(project_id, actor_id=admin.id)

    (entry,) = list(store.list_audit_log())
    assert entry.user_id == admin.id
    assert entry.action == models.AuditAction.SOFT_DELETE
    assert entry.entity_type == models.EntityType.PROJECT
    assert entry.entity_id == project_id
    details = json.loads(entry.details)
    assert details["Experiment"] == 1
    assert details["Run"] == 0
    assert details["Artifact"] == 0


def test_restore_project_records_a_restore_audit_entry(store: SQLLiteStore, admin: models.User) -> None:
    project_id, _experiment_id = _project_with_one_experiment(store)
    store.delete_project(project_id, actor_id=admin.id)

    store.restore_project(project_id, actor_id=admin.id)

    entries = list(store.list_audit_log())
    restore_entries = [e for e in entries if e.action == models.AuditAction.RESTORE]
    assert len(restore_entries) == 1
    assert restore_entries[0].entity_type == models.EntityType.PROJECT
    assert restore_entries[0].entity_id == project_id
    assert json.loads(restore_entries[0].details)["Experiment"] == 1


def test_purge_project_records_a_purge_audit_entry(store: SQLLiteStore, admin: models.User) -> None:
    project_id, _experiment_id = _project_with_one_experiment(store)
    store.delete_project(project_id, actor_id=admin.id)

    store.purge_project(project_id, admin)

    entries = list(store.list_audit_log())
    purge_entries = [e for e in entries if e.action == models.AuditAction.PURGE]
    assert len(purge_entries) == 1
    assert purge_entries[0].user_id == admin.id
    assert purge_entries[0].entity_type == models.EntityType.PROJECT
    assert purge_entries[0].entity_id == project_id
    assert json.loads(purge_entries[0].details)["Experiment"] == 1


def test_delete_run_records_zero_cascade_counts_when_nothing_depends_on_it(
    store: SQLLiteStore, admin: models.User
) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    run = store.create_run(models.NewRun(experiment_id=experiment.id))

    store.delete_run(run.id, actor_id=admin.id)

    (entry,) = list(store.list_audit_log())
    assert entry.entity_type == models.EntityType.RUN
    assert entry.entity_id == run.id
    assert json.loads(entry.details) == {"Artifact": 0}


def test_delete_project_does_not_double_count_a_child_already_independently_deleted(
    store: SQLLiteStore, admin: models.User
) -> None:
    """An experiment already deleted on its own must not be recounted in the project's cascade."""
    project = store.create_project(models.NewProject(name="p", description="d"))
    store.create_experiment(models.NewExperiment(project_id=project.id, name="kept"))
    already_deleted = store.create_experiment(models.NewExperiment(project_id=project.id, name="gone"))
    store.delete_experiment(already_deleted.id, actor_id=admin.id)

    store.delete_project(project.id, actor_id=admin.id)

    entries = list(store.list_audit_log())
    project_delete = next(e for e in entries if e.entity_type == models.EntityType.PROJECT)
    assert json.loads(project_delete.details)["Experiment"] == 1, (
        "only `kept` should be counted -- `already_deleted` was excluded by the "
        "'not already deleted' cascade guard"
    )


def test_list_audit_log_orders_most_recent_first(store: SQLLiteStore, admin: models.User) -> None:
    project_a = store.create_project(models.NewProject(name="a", description="d"))
    project_b = store.create_project(models.NewProject(name="b", description="d"))

    store.delete_project(project_a.id, actor_id=admin.id)
    store.delete_project(project_b.id, actor_id=admin.id)

    entries = list(store.list_audit_log())
    assert [e.entity_id for e in entries] == [project_b.id, project_a.id]


def test_list_audit_log_respects_limit_and_offset(store: SQLLiteStore, admin: models.User) -> None:
    projects = [store.create_project(models.NewProject(name=str(i), description="d")) for i in range(3)]
    for project in projects:
        store.delete_project(project.id, actor_id=admin.id)

    first_page = list(store.list_audit_log(limit=2, offset=0))
    second_page = list(store.list_audit_log(limit=2, offset=2))

    assert len(first_page) == 2
    assert len(second_page) == 1
    assert {e.id for e in first_page} | {e.id for e in second_page} == {
        e.id for e in store.list_audit_log(limit=100)
    }
