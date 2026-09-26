"""Tests for `FSArtifactStore`: deleting blobs, and recording the refs of blobs it has written."""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import TYPE_CHECKING

from pydantic import AnyUrl

from dltrack import models
from dltrack.plugins.data_stores.filesystem import FSArtifactStore
from dltrack.serve import set_data_store

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from dltrack.plugins.data_stores.sqlite import SQLLiteStore


def test_delete_artifact_removes_an_existing_blob(tmp_path: Path) -> None:
    store = FSArtifactStore.get_or_create(tmp_path, 1)
    target = tmp_path / "3" / "7" / "keyhash" / "0" / "namehash.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"data")
    ref = AnyUrl(f"{store.protocol}:///{target.relative_to(tmp_path)}")

    store.delete_artifact(ref)

    assert not target.exists()


def test_delete_artifact_is_a_noop_for_a_blob_that_is_already_gone(tmp_path: Path) -> None:
    store = FSArtifactStore.get_or_create(tmp_path, 1)
    ref = AnyUrl(f"{store.protocol}:///never/existed.png")

    store.delete_artifact(ref)  # must not raise


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


def _ingest(fs: FSArtifactStore, data_store: object, artifacts: list[models.Artifact]) -> None:
    """Queue `artifacts` as though `_save_artifact` just wrote their blobs, then start ingesting them."""
    for artifact in artifacts:
        fs._saved_artifact_q.put(artifact)  # pyright: ignore[reportPrivateUsage]
    app = _FakeApp()
    set_data_store(app, data_store)  # pyright: ignore[reportArgumentType]
    threading.Thread(target=fs.ingest_stored_artifacts, args=(app,), daemon=True).start()


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

    _ingest(FSArtifactStore.get_or_create(tmp_path, 10), _LockedOnceStore(store), [_artifact(run, "a.png")])

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
        FSArtifactStore.get_or_create(tmp_path, 10),
        store,
        [_artifact(kept_run, "kept.png"), _artifact(deleted_run, orphan.name)],
    )

    _wait_until(lambda: not orphan.exists())
    assert [a.run_id for a in store.fetch_artifacts(experiment_id)] == [kept_run.id]
