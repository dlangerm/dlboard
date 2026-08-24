# pyright: reportPrivateUsage=false
"""Tests for `_drain_pending`: the pure, synchronous core of the background purge worker."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Self

from dltrack import models
from dltrack.plugins.backend import artifact_purge_worker

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from flask import Response
    from pydantic import AnyUrl
    from werkzeug.datastructures import FileStorage

    from dltrack.plugins.data_stores.sqlite import SQLLiteStore


class _RaisingArtifactStore:
    """A minimal `ArtifactStore` stand-in that deletes every ref except those in `bad_refs`."""

    protocol: ClassVar[str] = "file"

    def __init__(self, bad_refs: set[str]) -> None:
        self.bad_refs = bad_refs
        self.deleted: list[str] = []

    @classmethod
    def get_or_create(cls, bad_refs: set[str]) -> Self:
        return cls(bad_refs)

    def log_artifacts(
        self, artifacts: Iterable[models.NewArtifact], files: Mapping[str, FileStorage]
    ) -> None:
        raise NotImplementedError

    def download_artifact(self, ref: AnyUrl) -> Response:
        raise NotImplementedError

    def delete_artifact(self, ref: AnyUrl) -> None:
        if str(ref) in self.bad_refs:
            msg = "boom"
            raise RuntimeError(msg)
        self.deleted.append(str(ref))


def _project_experiment_run(store: SQLLiteStore) -> tuple[int, int]:
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    run = store.create_run(models.NewRun(experiment_id=experiment.id))
    return project.id, run.id


def _queue_purge_tasks(store: SQLLiteStore, count: int) -> int:
    """Soft-delete then purge a project with `count` artifacts, queuing one task per artifact."""
    admin = store.get_or_create_user("admin")
    project_id, run_id = _project_experiment_run(store)
    experiment_id = next(store.get_experiments(project_id)).id
    store.log_artifact_refs(
        [
            models.Artifact(
                key=f"a{i}",
                fname=f"a{i}.png",
                run_id=run_id,
                experiment_id=experiment_id,
                step=i,
                ref=f"file:///a{i}.png",
            )
            for i in range(count)
        ]
    )
    store.delete_project(project_id, admin)
    store.purge_project(project_id, admin)
    return admin.id


def test_drain_pending_deletes_every_blob_and_clears_the_queue(store: SQLLiteStore) -> None:
    _queue_purge_tasks(store, 3)
    artifact_store = _RaisingArtifactStore(bad_refs=set())

    artifact_purge_worker._drain_pending(store, artifact_store)

    assert store.count_pending_artifact_purges() == 0
    assert sorted(artifact_store.deleted) == ["file:///a0.png", "file:///a1.png", "file:///a2.png"]


def test_drain_pending_records_a_failure_and_still_processes_the_rest(store: SQLLiteStore) -> None:
    _queue_purge_tasks(store, 3)
    artifact_store = _RaisingArtifactStore(bad_refs={"file:///a1.png"})

    artifact_purge_worker._drain_pending(store, artifact_store)

    remaining = list(store.list_pending_artifact_purges())
    assert [t.ref for t in remaining] == ["file:///a1.png"]
    assert remaining[0].last_error == "boom"
    assert sorted(artifact_store.deleted) == ["file:///a0.png", "file:///a2.png"]


def test_drain_pending_is_a_noop_when_nothing_is_queued(store: SQLLiteStore) -> None:
    artifact_store = _RaisingArtifactStore(bad_refs=set())

    artifact_purge_worker._drain_pending(store, artifact_store)

    assert artifact_store.deleted == []
