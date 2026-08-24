# pyright: reportPrivateUsage=false
"""Tests for the REST route handler functions: actor resolution, `created_by` stamping, and the
soft-delete/restore handlers -- all against a real store, without needing a live Flask request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import pytest

from dltrack import models
from dltrack.plugins.backend import basic_rest_backend as backend

if TYPE_CHECKING:
    from flask import Response
    from pydantic import AnyUrl

    from dltrack.plugins.data_stores.sqlite import SQLLiteStore


class _FakeArtifactStore:
    """A minimal `ArtifactStore` stand-in that just records what it was asked to log."""

    protocol: ClassVar[str] = "fake"

    def __init__(self) -> None:
        self.logged: list[models.NewArtifact] = []

    @classmethod
    def get_or_create(cls) -> _FakeArtifactStore:
        return cls()

    def log_artifacts(self, artifacts: object, files: object) -> None:
        self.logged = list(artifacts)  # pyright: ignore[reportArgumentType]

    def download_artifact(self, ref: AnyUrl) -> Response:
        raise NotImplementedError

    def delete_artifact(self, ref: AnyUrl) -> None:
        raise NotImplementedError


@pytest.mark.parametrize("header_value", [None, "", "   "])
def test_resolve_actor_falls_back_to_anonymous_for_missing_or_blank_header(
    store: SQLLiteStore, header_value: str | None
) -> None:
    actor = backend.resolve_actor(store, header_value)
    assert actor.username == "anonymous"


def test_resolve_actor_creates_and_reuses_a_user_for_the_header_value(store: SQLLiteStore) -> None:
    first = backend.resolve_actor(store, "alice")
    second = backend.resolve_actor(store, "alice")

    assert first == second
    assert first.username == "alice"


def test_handle_create_project_stamps_created_by(store: SQLLiteStore) -> None:
    body = models.NewProject(name="p", description="d").model_dump(mode="json")

    result = backend.handle_create_project(store, body, "alice")

    actor = backend.resolve_actor(store, "alice")
    assert result["created_by"] == actor.id
    assert store.get_project(result["id"]).created_by == actor.id


def test_handle_create_project_attributes_to_anonymous_without_a_header(store: SQLLiteStore) -> None:
    body = models.NewProject(name="p", description="d").model_dump(mode="json")

    result = backend.handle_create_project(store, body, None)

    anonymous = backend.resolve_actor(store, None)
    assert anonymous.username == "anonymous"
    assert result["created_by"] == anonymous.id


def test_handle_create_experiment_stamps_created_by(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    body = models.NewExperiment(project_id=project.id).model_dump(mode="json")

    result = backend.handle_create_experiment(store, body, "bob")

    actor = backend.resolve_actor(store, "bob")
    assert result["created_by"] == actor.id


def test_handle_create_run_stamps_created_by(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    body = models.NewRun(experiment_id=experiment.id).model_dump(mode="json")

    result = backend.handle_create_run(store, body, "carol")

    actor = backend.resolve_actor(store, "carol")
    assert result["created_by"] == actor.id


def test_handle_log_artifacts_stamps_created_by_on_every_artifact(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    run = store.create_run(models.NewRun(experiment_id=experiment.id))
    artifact_store = _FakeArtifactStore()
    new_artifacts = [
        models.NewArtifact(key="img", fname="a.png", run_id=run.id, experiment_id=experiment.id, step=0),
        models.NewArtifact(key="other", fname="b.bin", run_id=run.id, experiment_id=experiment.id, step=0),
    ]

    backend.handle_log_artifacts(artifact_store, store, new_artifacts, files={}, header_value="dave")

    actor = backend.resolve_actor(store, "dave")
    assert len(artifact_store.logged) == 2
    assert all(a.created_by == actor.id for a in artifact_store.logged)


def test_handle_delete_and_restore_project_attribute_the_actor(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))

    backend.handle_delete_project(store, project.id, "erin")

    actor = backend.resolve_actor(store, "erin")
    assert project.id not in {p.id for p in store.get_projects()}
    (entry,) = list(store.list_audit_log())
    assert entry.user_id == actor.id
    assert entry.action == models.AuditAction.SOFT_DELETE

    backend.handle_restore_project(store, project.id, "erin")

    assert project.id in {p.id for p in store.get_projects()}


def test_handle_delete_and_restore_experiment_attribute_the_actor(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))

    backend.handle_delete_experiment(store, experiment.id, "frank")
    assert store.get_experiment(experiment.id) is None

    backend.handle_restore_experiment(store, experiment.id, "frank")
    assert store.get_experiment(experiment.id) is not None


def test_handle_delete_and_restore_run_attribute_the_actor(store: SQLLiteStore, experiment_id: int) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))

    backend.handle_delete_run(store, run.id, "gina")
    assert store._fetch_deleted_at(models.Run, run.id) is not None

    backend.handle_restore_run(store, run.id, "gina")
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

    backend.handle_delete_artifact(store, artifact.id, "hank")
    assert list(store.fetch_artifacts(experiment_id=experiment_id)) == []

    backend.handle_restore_artifact(store, artifact.id, "hank")
    assert len(list(store.fetch_artifacts(experiment_id=experiment_id))) == 1
