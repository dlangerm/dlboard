"""Tests for `_drain_pending`: the pure, synchronous core of the background purge worker."""

from __future__ import annotations

import threading
import time
import typing
from typing import TYPE_CHECKING, Self

from dlboard import models
from dlboard.conftest import create_entity_chain, scraped_total
from dlboard.plugins.backend import artifact_purge_worker
from dlboard.serve.telemetry._prometheus import serve_metrics

if TYPE_CHECKING:
    from collections.abc import Iterable

    import pytest
    from dash import Dash
    from flask import Response
    from pydantic import AnyUrl
    from werkzeug.datastructures import FileStorage

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


class _RaisingArtifactStore:
    """A minimal `ArtifactStore` stand-in that deletes every ref except those in `bad_refs`."""

    def __init__(self, bad_refs: set[str]) -> None:
        self.bad_refs = bad_refs
        self.deleted: list[str] = []

    @classmethod
    def get_or_create(cls, bad_refs: set[str]) -> Self:
        return cls(bad_refs)

    def log_artifacts(self, artifacts: Iterable[tuple[models.NewArtifact, FileStorage]]) -> None:
        raise NotImplementedError

    def link_artifacts(self, links: Iterable[tuple[models.NewArtifact, AnyUrl]]) -> list[models.Artifact]:
        raise NotImplementedError

    def download_artifact(self, ref: AnyUrl) -> Response:
        raise NotImplementedError

    def delete_artifact(self, ref: AnyUrl) -> None:
        if str(ref) in self.bad_refs:
            msg = "boom"
            raise RuntimeError(msg)
        self.deleted.append(str(ref))


def _queue_purge_tasks(store: SQLLiteStore, count: int) -> int:
    """Soft-delete then purge a project with `count` artifacts, queuing one task per artifact."""
    admin = store.get_or_create_user(models.Principal.unverified("admin"))
    project_id, experiment_id, run_id, _artifact_id = create_entity_chain(store)
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


def test_the_store_counts_each_purge_by_whether_its_blob_was_deleted(
    store: SQLLiteStore, telemetry: pytest.MonkeyPatch
) -> None:
    telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "prometheus")
    _queue_purge_tasks(store, 3)

    artifact_purge_worker._drain_pending(store, _RaisingArtifactStore(bad_refs={"file:///a1.png"}))

    scrape = serve_metrics().get_data(as_text=True)
    assert scraped_total(scrape, "dlboard_artifact_purges_total", dlboard_outcome="success") == 2
    assert scraped_total(scrape, "dlboard_artifact_purges_total", dlboard_outcome="failure") == 1


def test_purging_what_is_purged_already_does_nothing_and_queues_no_blob_twice(store: SQLLiteStore) -> None:
    """A second click, or another process, purging the same entity (#429)."""
    admin = store.get_or_create_user(models.Principal.unverified("admin"))
    project_id, *_ = create_entity_chain(store)
    store.delete_project(project_id, admin)
    store.purge_project(project_id, admin)
    pending = store.count_pending_artifact_purges()

    store.purge_project(project_id, admin)

    assert store.count_pending_artifact_purges() == pending
    purges = [entry for entry in store.list_audit_log(admin) if entry.action is models.AuditAction.PURGE]
    assert len(purges) == 1


class _WatchingArtifactStore(_RaisingArtifactStore):
    """Notes, at each blob it deletes, whether the store says a purge is running."""

    def __init__(self, store: SQLLiteStore) -> None:
        super().__init__(bad_refs=set())
        self.store = store
        self.running: list[bool] = []

    @typing.override
    def delete_artifact(self, ref: AnyUrl) -> None:
        self.running.append(self.store.artifact_purge_progress_at() is not None)
        super().delete_artifact(ref)


def test_a_drain_says_a_purge_is_running_until_it_is_done(store: SQLLiteStore) -> None:
    """Every server process reads it, to tell a purge in progress from one a process died in (#429)."""
    _queue_purge_tasks(store, 3)
    artifact_store = _WatchingArtifactStore(store)

    artifact_purge_worker._drain_pending(store, artifact_store)

    assert artifact_store.running == [True, True, True]
    assert store.artifact_purge_progress_at() is None


def test_a_failed_drain_is_logged_and_the_worker_still_runs_the_next_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One drain that raised used to end the worker thread, so no later purge ran until a restart (CI, #455's tests)."""
    drains: list[int] = []

    def drain(_store: object, _artifact_store: object) -> None:
        drains.append(len(drains))
        if len(drains) == 1:
            msg = "the database is gone"
            raise RuntimeError(msg)
        if len(drains) == 3:
            raise SystemExit  # ends the worker, which a test cannot otherwise stop

    def no_store(_app: Dash) -> None:
        """The worker's store waits, which a test's stores need not."""

    monkeypatch.setattr(artifact_purge_worker, "_drain_pending", drain)
    monkeypatch.setattr(artifact_purge_worker, "wait_for_data_store", no_store)
    monkeypatch.setattr(artifact_purge_worker, "wait_for_artifact_store", no_store)
    worker = threading.Thread(target=artifact_purge_worker._run, args=(None,), daemon=True)
    worker.start()

    for _ in range(500):
        if len(drains) >= 2:
            break
        artifact_purge_worker.wake()
        time.sleep(0.01)

    assert len(drains) >= 2
    worker.join(timeout=5)
