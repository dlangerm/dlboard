# pyright: reportPrivateUsage=false
"""Tests for the REST route handler functions: `created_by` stamping and the soft-delete/restore
handlers -- all against a real store and an already-resolved actor, without needing a live Flask
request or auth provider.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import AnyUrl
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import NotFound

from dltrack import models
from dltrack.plugins.backend import basic_rest_backend as backend

if TYPE_CHECKING:
    from collections.abc import Iterable

    from flask import Response

    from dltrack.plugins.data_stores.sqlite import SQLLiteStore


class _FakeArtifactStore:
    """A minimal `ArtifactStore` stand-in that just records what it was asked to log."""

    def __init__(self) -> None:
        self.logged: list[models.NewArtifact] = []

    @classmethod
    def get_or_create(cls) -> _FakeArtifactStore:
        return cls()

    def log_artifacts(self, artifacts: Iterable[tuple[models.NewArtifact, FileStorage]]) -> None:
        self.logged = [artifact for artifact, _ in artifacts]

    def link_artifacts(self, links: Iterable[tuple[models.NewArtifact, AnyUrl]]) -> list[models.Artifact]:
        linked = [models.Artifact.model_validate(a.model_dump() | {"ref": str(ref)}) for a, ref in links]
        self.logged = [a for a, _ in links]
        return linked

    def download_artifact(self, ref: AnyUrl) -> Response:
        raise NotImplementedError

    def delete_artifact(self, ref: AnyUrl) -> None:
        raise NotImplementedError


def test_handle_create_project_stamps_created_by(store: SQLLiteStore) -> None:
    body = models.NewProject(name="p", description="d").model_dump(mode="json")
    actor = store.get_or_create_user(models.Principal.unverified("alice"))

    result = backend.handle_create_project(store, body, actor)

    assert result["created_by"] == actor.id
    assert store.get_project(result["id"]) == models.Project.model_validate(result)


def test_handle_create_experiment_stamps_created_by(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    body = models.NewExperiment(project_id=project.id).model_dump(mode="json")
    actor = store.get_or_create_user(models.Principal.unverified("bob"))

    result = backend.handle_create_experiment(store, body, actor)

    assert result["created_by"] == actor.id


def test_handle_get_or_create_project_creates_on_first_call(store: SQLLiteStore) -> None:
    body = backend.GetOrCreateProject(name="p", description="d").model_dump(mode="json")
    actor = store.get_or_create_user(models.Principal.unverified("alice"))

    result = backend.handle_get_or_create_project(store, body, actor)

    assert result["name"] == "p"
    assert result["created_by"] == actor.id


def test_handle_get_or_create_project_reuses_an_existing_project_by_name(store: SQLLiteStore) -> None:
    existing = store.create_project(models.NewProject(name="p", description="d"))
    body = backend.GetOrCreateProject(name="p", description="ignored").model_dump(mode="json")
    actor = store.get_or_create_user(models.Principal.unverified("alice"))

    result = backend.handle_get_or_create_project(store, body, actor)

    assert result["id"] == existing.id
    assert result["description"] == "d"


def test_handle_get_or_create_experiment_creates_on_first_call(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    body = backend.GetOrCreateExperiment(project_id=project.id, name="default").model_dump(mode="json")
    actor = store.get_or_create_user(models.Principal.unverified("bob"))

    result = backend.handle_get_or_create_experiment(store, body, actor)

    assert result["name"] == "default"
    assert result["created_by"] == actor.id


def test_handle_get_or_create_experiment_passes_source_through(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    body = backend.GetOrCreateExperiment(
        project_id=project.id, name="default", source=models.ExperimentSource.PYTORCH_LIGHTNING
    ).model_dump(mode="json")
    actor = store.get_or_create_user(models.Principal.unverified("bob"))

    result = backend.handle_get_or_create_experiment(store, body, actor)

    assert result["source"] == models.ExperimentSource.PYTORCH_LIGHTNING


def test_handle_get_or_create_experiment_reuses_an_existing_experiment_by_name(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    existing = store.create_experiment(models.NewExperiment(project_id=project.id, name="default"))
    body = backend.GetOrCreateExperiment(project_id=project.id, name="default").model_dump(mode="json")
    actor = store.get_or_create_user(models.Principal.unverified("bob"))

    result = backend.handle_get_or_create_experiment(store, body, actor)

    assert result["id"] == existing.id


def test_handle_create_run_stamps_created_by(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    body = models.NewRun(experiment_id=experiment.id).model_dump(mode="json")
    actor = store.get_or_create_user(models.Principal.unverified("carol"))

    result = backend.handle_create_run(store, body, actor)

    assert result["created_by"] == actor.id


def test_handle_get_run_returns_a_live_run_and_404s_otherwise(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    deleted = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.delete_run(deleted.id, store.get_or_create_user(models.Principal.unverified("heidi")))

    assert models.Run.model_validate(backend.handle_get_run(store, run.id)) == run
    for missing in (deleted.id, run.id + 1000):
        with pytest.raises(NotFound):
            backend.handle_get_run(store, missing)


def test_handle_log_artifacts_stamps_created_by_on_every_artifact(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    run = store.create_run(models.NewRun(experiment_id=experiment.id))
    artifact_store = _FakeArtifactStore()
    actor = store.get_or_create_user(models.Principal.unverified("dave"))
    new_artifacts = [
        models.NewArtifact(key="img", fname="a.png", run_id=run.id, experiment_id=experiment.id, step=0),
        models.NewArtifact(key="other", fname="b.bin", run_id=run.id, experiment_id=experiment.id, step=0),
    ]

    backend.handle_log_artifacts(
        artifact_store, [(artifact, FileStorage()) for artifact in new_artifacts], actor=actor
    )

    assert len(artifact_store.logged) == 2
    assert all(a.created_by == actor.id for a in artifact_store.logged)


def test_handle_link_artifacts_stamps_created_by_and_records_refs(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    run = store.create_run(models.NewRun(experiment_id=experiment.id))
    artifact_store = _FakeArtifactStore()
    actor = store.get_or_create_user(models.Principal.unverified("dave"))
    links = [
        models.NewArtifactLink(
            key="img",
            fname="a.png",
            run_id=run.id,
            experiment_id=experiment.id,
            step=0,
            ref=AnyUrl("s3://bucket/a.png"),
        )
    ]

    backend.handle_link_artifacts(store, artifact_store, links, actor=actor)

    (stored,) = store.fetch_artifacts(experiment_id=experiment.id)
    assert stored.ref == "s3://bucket/a.png"
    assert stored.created_by == actor.id


def test_handle_delete_and_restore_project_attribute_the_actor(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    actor = store.get_or_create_user(models.Principal.unverified("erin"))

    backend.handle_delete_project(store, project.id, actor)

    assert project.id not in {p.id for p in store.get_projects()}
    (entry,) = list(store.list_audit_log(actor))
    assert entry.user_id == actor.id
    assert entry.action == models.AuditAction.SOFT_DELETE

    backend.handle_restore_project(store, project.id, actor)

    assert project.id in {p.id for p in store.get_projects()}


def test_handle_delete_and_restore_experiment_attribute_the_actor(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    actor = store.get_or_create_user(models.Principal.unverified("frank"))

    backend.handle_delete_experiment(store, experiment.id, actor)
    assert store.get_experiment(experiment.id) is None

    backend.handle_restore_experiment(store, experiment.id, actor)
    assert store.get_experiment(experiment.id) is not None


def test_handle_delete_and_restore_run_attribute_the_actor(store: SQLLiteStore, experiment_id: int) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    actor = store.get_or_create_user(models.Principal.unverified("gina"))

    backend.handle_delete_run(store, run.id, actor)
    assert store._fetch_deleted_at(models.Run, run.id) is not None

    backend.handle_restore_run(store, run.id, actor)
    assert store._fetch_deleted_at(models.Run, run.id) is None


def test_handle_delete_and_restore_artifact_attribute_the_actor(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_artifact_refs(
        [
            models.Artifact(
                key="img", fname="a.png", run_id=run.id, experiment_id=experiment_id, step=0, ref="ref://a"
            )
        ]
    )
    (artifact,) = list(store.fetch_artifacts(experiment_id=experiment_id))
    assert artifact.id is not None
    actor = store.get_or_create_user(models.Principal.unverified("hank"))

    backend.handle_delete_artifact(store, artifact.id, actor)
    assert list(store.fetch_artifacts(experiment_id=experiment_id)) == []

    backend.handle_restore_artifact(store, artifact.id, actor)
    assert len(list(store.fetch_artifacts(experiment_id=experiment_id))) == 1
