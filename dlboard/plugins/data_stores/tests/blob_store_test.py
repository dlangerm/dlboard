"""
Tests for `BlobArtifactStore`: deleting blobs, linking refs, and recording refs it has written.

Tests that only need `BlobBackend`'s own interface (`ref_for`/`exists`/`write`) are parametrized
over every backend via `EVERY_ARTIFACT_BACKEND` -- behavior any backend must get right. Tests
that necessarily reach into one backend's own storage (a filesystem traversal attempt, the
ingest-thread pipeline that only `FSBlobs` happens to exercise here) stay filesystem-only.
"""

from __future__ import annotations

import io
import os
import sqlite3
import time
from typing import TYPE_CHECKING, override
from uuid import uuid4

import pytest
from pydantic import AnyUrl
from werkzeug.datastructures import FileStorage

from dlboard import models
from dlboard.conftest import EVERY_ARTIFACT_BACKEND, ArtifactBackend
from dlboard.plugins.data_stores._blob_store import BlobArtifactStore, _waited_since, blob_key
from dlboard.plugins.data_stores.filesystem import FSBlobs
from dlboard.plugins.data_stores.tests.conftest import file, ingest, rejections, upload, wait_until

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from dlboard.conftest import MakeBlobStore
    from dlboard.plugins.data_stores._blob_store import BlobBackend
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


@pytest.fixture(params=EVERY_ARTIFACT_BACKEND)
def artifact_backend(request: pytest.FixtureRequest) -> ArtifactBackend:
    """Override the default (filesystem-only) fixture: every test below runs on every backend."""
    return request.param


def _write(backend: BlobBackend, key: str, content: bytes, tmp_path: Path) -> AnyUrl:
    """Place `content` at `key` through the backend's own `write`, as if a prior upload had."""
    staged = tmp_path / "staged"
    staged.write_bytes(content)
    ref = backend.ref_for(blob_key(1, 1, "img", 0, key))
    backend.write(staged, ref)
    return ref


def test_delete_artifact_removes_an_existing_blob(
    blob_backend: BlobBackend, tmp_path: Path, make_blob_store: MakeBlobStore
) -> None:
    store = make_blob_store(blob_backend, 1)
    ref = _write(blob_backend, "a.png", b"data", tmp_path)

    store.delete_artifact(ref)

    assert not blob_backend.exists(ref)


def test_delete_artifact_is_a_noop_for_a_blob_that_is_already_gone(
    blob_backend: BlobBackend, make_blob_store: MakeBlobStore
) -> None:
    store = make_blob_store(blob_backend, 1)
    ref = blob_backend.ref_for(blob_key(1, 1, "img", 0, "never-existed.png"))

    store.delete_artifact(ref)  # must not raise


def test_log_artifacts_rejects_a_duplicate_key_fname(
    blob_backend: BlobBackend, tmp_path: Path, make_blob_store: MakeBlobStore
) -> None:
    """
    The duplicate check must look at the same (suffixed) ref the write worker actually writes to.

    A blob already sitting at that ref -- placed directly here, not through the async write worker,
    so this doesn't race it -- must reject a second upload for the same key/fname synchronously.
    """
    store = make_blob_store(blob_backend, 10)
    _write(blob_backend, "a.png", b"data", tmp_path)
    new_artifact = models.NewArtifact(key="img", fname="a.png", run_id=1, experiment_id=1, step=0)

    with pytest.raises(ValueError, match="duplicate"):
        store.log_artifacts([(new_artifact, FileStorage(io.BytesIO(b"x"), filename="a.png"))])


def test_link_artifacts_accepts_a_ref_already_written_inside_the_stores_own_space(
    blob_backend: BlobBackend, tmp_path: Path, make_blob_store: MakeBlobStore
) -> None:
    store = make_blob_store(blob_backend, 10)
    ref = _write(blob_backend, "a.png", b"data", tmp_path)
    new_artifact = models.NewArtifact(key="img", fname="a.png", run_id=1, experiment_id=1, step=0)

    (linked,) = store.link_artifacts([(new_artifact, ref)])

    assert linked.ref == str(ref)


def test_link_artifacts_rejects_a_ref_nothing_is_actually_stored_at(
    blob_backend: BlobBackend, make_blob_store: MakeBlobStore
) -> None:
    """A ref inside the store's own space that no blob was ever written to still isn't linkable."""
    store = make_blob_store(blob_backend, 10)
    new_artifact = models.NewArtifact(key="img", fname="missing.png", run_id=1, experiment_id=1, step=0)
    ref = blob_backend.ref_for(blob_key(1, 1, "img", 0, "missing.png"))

    with pytest.raises(models.UnservableArtifactRefError):
        store.link_artifacts([(new_artifact, ref)])


# -- Filesystem-specific --------------------------------------------------------------------------
#
# A path-traversal attempt and the ingest-thread pipeline below are either specific to how
# `FSBlobs` resolves a ref against a root directory, or (the ingest tests) exercise logic shared
# by every `BlobArtifactStore` regardless of backend -- already covered generically above, these
# just happen to use `FSBlobs` as *a* backend to drive them.


def test_delete_artifact_is_a_noop_for_a_ref_outside_the_stores_own_space(
    tmp_path: Path, make_blob_store: MakeBlobStore
) -> None:
    """A backend must never delete (or serve) something outside its own space, even on request."""
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"data")
    store = make_blob_store(FSBlobs(tmp_path / "artifacts"), 1)

    store.delete_artifact(AnyUrl(f"file:///../{outside.name}"))

    assert outside.exists()


def test_link_artifacts_rejects_a_ref_outside_the_stores_own_space(
    tmp_path: Path, make_blob_store: MakeBlobStore
) -> None:
    store = make_blob_store(FSBlobs(tmp_path), 10)
    new_artifact = models.NewArtifact(key="img", fname="passwd", run_id=1, experiment_id=1, step=0)

    with pytest.raises(models.UnservableArtifactRefError):
        store.link_artifacts([(new_artifact, AnyUrl("file:///etc/passwd"))])


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


def test_a_transient_store_failure_does_not_lose_the_batch(
    store: SQLLiteStore, experiment_id: int, tmp_path: Path, make_blob_store: MakeBlobStore
) -> None:
    """The ingest loop used to clear its batch on *any* failure, so a locked db lost artifacts for good."""
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    artifact_store = make_blob_store(FSBlobs(tmp_path / "artifacts"), 10)
    ingest(artifact_store, _LockedOnceStore(store))

    upload(artifact_store, run, "a.png")

    wait_until(lambda: len(list(store.fetch_artifacts(experiment_id, run_ids=None))) == 1)


def test_a_rejected_artifact_is_dropped_alone_and_its_blob_deleted(
    store: SQLLiteStore,
    experiment_id: int,
    admin: models.User,
    tmp_path: Path,
    make_blob_store: MakeBlobStore,
) -> None:
    """One rejected artifact used to take its whole batch down with it, orphaning every blob in it."""
    kept_run, deleted_run = (store.create_run(models.NewRun(experiment_id=experiment_id)) for _ in range(2))
    store.delete_run(deleted_run.id, admin)
    blobs = FSBlobs(tmp_path / "artifacts")
    artifact_store = make_blob_store(blobs, 10)
    ingest(artifact_store, store)

    artifact_store.log_artifacts([file(kept_run, "kept.png"), file(deleted_run, "orphan.png")])

    wait_until(lambda: artifact_store._journal.held() == [])
    assert [a.run_id for a in store.fetch_artifacts(experiment_id, run_ids=None)] == [kept_run.id]
    assert not blobs.exists(blobs.ref_for(blob_key(experiment_id, deleted_run.id, "img", 0, "orphan.png")))


# -- Shutdown and staging -------------------------------------------------------------------------


def test_close_writes_every_queued_blob_and_records_its_ref_before_returning(
    store: SQLLiteStore, experiment_id: int, tmp_path: Path, make_blob_store: MakeBlobStore
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    blobs = FSBlobs(tmp_path / "artifacts")
    artifact_store = make_blob_store(blobs, 50)
    ingest(artifact_store, store)
    wait_until(artifact_store._ingest_ready.is_set)

    for i in range(20):
        upload(artifact_store, run, f"{i}.png")
    artifact_store.close()

    assert len(list(store.fetch_artifacts(experiment_id, run_ids=None))) == 20


def test_uploads_are_turned_away_once_the_store_is_disposed(
    tmp_path: Path, telemetry: pytest.MonkeyPatch, make_blob_store: MakeBlobStore
) -> None:
    telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "prometheus")
    store = make_blob_store(FSBlobs(tmp_path), 10)
    store.dispose()

    with pytest.raises(models.ArtifactStoreUnavailableError):
        upload(store, models.Run.model_construct(id=1, experiment_id=1), "a.png")

    assert rejections("writer_down") == 1


def test_a_failed_upload_leaves_nothing_in_the_staging_dir(
    tmp_path: Path, telemetry: pytest.MonkeyPatch
) -> None:
    telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "prometheus")

    class _Broken(io.BytesIO):
        @override
        def read(self, *_args: object) -> bytes:
            msg = "client hung up"
            raise ConnectionError(msg)

    staging = tmp_path / "staging"
    store = BlobArtifactStore(FSBlobs(tmp_path / "artifacts"), 10, staging)
    new_artifact = models.NewArtifact(key="img", fname="a.png", run_id=1, experiment_id=1, step=0)

    with pytest.raises(ConnectionError):
        store.log_artifacts([(new_artifact, FileStorage(_Broken(), filename="a.png"))])

    assert list(staging.iterdir()) == []
    assert rejections("failed") == 1
    store.dispose()


def test_stale_staged_uploads_from_a_dead_process_are_swept_on_startup(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    stale, fresh = staging / str(uuid4()), staging / str(uuid4())
    stale.write_bytes(b"x")
    fresh.write_bytes(b"x")
    os.utime(stale, (0, 0))

    BlobArtifactStore(FSBlobs(tmp_path / "artifacts"), 10, staging).dispose()

    assert [p.name for p in staging.iterdir()] == [fresh.name]


def test_the_sweep_leaves_a_file_it_did_not_stage_however_old(tmp_path: Path) -> None:
    """A staging directory may be one other programs use too: a file another program left there is not the store's."""
    staging = tmp_path / "staging"
    staging.mkdir()
    theirs = staging / "cpan_install_x.txt"
    theirs.write_bytes(b"x")
    os.utime(theirs, (0, 0))

    BlobArtifactStore(FSBlobs(tmp_path / "artifacts"), 10, staging).dispose()

    assert theirs.exists()


def test_a_queue_wait_is_never_negative_for_a_stamp_later_than_now() -> None:
    """A stamp later than this process's clock, as another process's can be, gives a zero wait, not a negative one."""
    assert _waited_since(time.monotonic() + 60) == 0.0
    assert _waited_since(time.monotonic() - 2) >= 2
