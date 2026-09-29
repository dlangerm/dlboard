"""Tests for `BlobArtifactStore` over `FSBlobs`: deleting blobs, and recording refs it has written."""

from __future__ import annotations

import io
import sqlite3
import threading
import time
from typing import TYPE_CHECKING

import pytest
from pydantic import AnyUrl
from werkzeug.datastructures import FileStorage

from dltrack import models
from dltrack.plugins.data_stores._blob_store import BlobArtifactStore, blob_key
from dltrack.plugins.data_stores.filesystem import FSBlobs
from dltrack.serve import set_data_store

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from dltrack.plugins.data_stores.sqlite import SQLLiteStore


def test_delete_artifact_removes_an_existing_blob(tmp_path: Path) -> None:
    store = BlobArtifactStore.get_or_create(FSBlobs(tmp_path), 1)
    target = tmp_path / "3" / "7" / "keyhash" / "0" / "namehash.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"data")
    ref = AnyUrl(f"file:///{target.relative_to(tmp_path)}")

    store.delete_artifact(ref)

    assert not target.exists()


def test_delete_artifact_is_a_noop_for_a_blob_that_is_already_gone(tmp_path: Path) -> None:
    store = BlobArtifactStore.get_or_create(FSBlobs(tmp_path), 1)
    ref = AnyUrl("file:///never/existed.png")

    store.delete_artifact(ref)  # must not raise


def test_delete_artifact_is_a_noop_for_a_ref_outside_the_stores_own_space(tmp_path: Path) -> None:
    """A backend must never delete (or serve) something outside its own space, even on request."""
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"data")
    store = BlobArtifactStore.get_or_create(FSBlobs(tmp_path / "artifacts"), 1)

    store.delete_artifact(AnyUrl(f"file:///../{outside.name}"))

    assert outside.exists()


class _FakeApp:
    """A bare stand-in for `Dash` -- the ingest thread only needs the data store set on it."""


class _LockedOnceStore:
    """Passes through to a real store, except its first `log_artifact_refs` fails like a locked sqlite db."""

    def __init__(self, inner: SQLLiteStore) -> None:
        self._inner = inner
        self._locked = True

    def log_artifact_refs(self, artifacts: Iterable[models.Artifact]) -> None:
        if self._locked:
            self._locked = False
            msg = "database is locked"
            raise sqlite3.OperationalError(msg)
        self._inner.log_artifact_refs(artifacts)


def _ingest(store: BlobArtifactStore, data_store: object, artifacts: list[models.Artifact]) -> None:
    """Queue `artifacts` as though `_write_blobs` just wrote their blobs, then start ingesting them."""
    for artifact in artifacts:
        store._saved_artifact_q.put(artifact)  # pyright: ignore[reportPrivateUsage]
    app = _FakeApp()
    set_data_store(app, data_store)  # pyright: ignore[reportArgumentType]
    threading.Thread(target=store.ingest_stored_artifacts, args=(app,), daemon=True).start()


def _wait_until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.1)


def _artifact(run: models.Run, name: str) -> models.Artifact:
    return models.Artifact(
        key="img", fname=name, run_id=run.id, experiment_id=run.experiment_id, step=0, ref=f"file:///{name}"
    )


def test_a_transient_store_failure_does_not_lose_the_batch(
    store: SQLLiteStore, experiment_id: int, tmp_path: Path
) -> None:
    """The ingest loop used to clear its batch on *any* failure, so a locked db lost artifacts for good."""
    run = store.create_run(models.NewRun(experiment_id=experiment_id))

    _ingest(
        BlobArtifactStore.get_or_create(FSBlobs(tmp_path), 10),
        _LockedOnceStore(store),
        [_artifact(run, "a.png")],
    )

    _wait_until(lambda: len(list(store.fetch_artifacts(experiment_id))) == 1)


def test_a_rejected_artifact_is_dropped_alone_and_its_blob_deleted(
    store: SQLLiteStore, experiment_id: int, admin: models.User, tmp_path: Path
) -> None:
    """One rejected artifact used to take its whole batch down with it, orphaning every blob in it."""
    kept_run, deleted_run = (store.create_run(models.NewRun(experiment_id=experiment_id)) for _ in range(2))
    store.delete_run(deleted_run.id, admin)
    orphan = tmp_path / "orphan.png"
    orphan.write_bytes(b"data")

    _ingest(
        BlobArtifactStore.get_or_create(FSBlobs(tmp_path), 10),
        store,
        [_artifact(kept_run, "kept.png"), _artifact(deleted_run, orphan.name)],
    )

    _wait_until(lambda: not orphan.exists())
    assert [a.run_id for a in store.fetch_artifacts(experiment_id)] == [kept_run.id]


def test_log_artifacts_rejects_a_duplicate_key_fname(tmp_path: Path) -> None:
    """
    The duplicate check must look at the same (suffixed) ref the write worker actually writes to.

    A blob already sitting at that ref -- placed directly here, not through the async write worker,
    so this doesn't race it -- must reject a second upload for the same key/fname synchronously.
    """
    backend = FSBlobs(tmp_path)
    store = BlobArtifactStore.get_or_create(backend, 10)
    new_artifact = models.NewArtifact(key="img", fname="a.png", run_id=1, experiment_id=1, step=0)
    existing_ref = backend.ref_for(blob_key(1, 1, "img", 0, "a.png"))
    existing_path = tmp_path / str(existing_ref.path).lstrip("/")
    existing_path.parent.mkdir(parents=True)
    existing_path.write_bytes(b"data")

    with pytest.raises(ValueError, match="duplicate"):
        store.log_artifacts([(new_artifact, FileStorage(io.BytesIO(b"x"), filename="a.png"))])
