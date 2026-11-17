"""
What answering an upload promises: the server has the file, and writes it and records its ref however its processes die,
once its storage and database are working again. No upload is lost, and none is recorded twice.
"""

from __future__ import annotations

import errno
import io
import threading
from typing import TYPE_CHECKING, Any, override

import pytest
from werkzeug.datastructures import FileStorage

from dlboard import models
from dlboard.conftest import scraped_total
from dlboard.plugins.data_stores import _blob_store
from dlboard.plugins.data_stores._blob_store import blob_key
from dlboard.plugins.data_stores._upload_journal import UploadJournal
from dlboard.plugins.data_stores.filesystem import FSBlobs
from dlboard.plugins.data_stores.tests.conftest import file, ingest, rejections, upload, wait_until
from dlboard.serve.telemetry._prometheus import serve_metrics

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pydantic import AnyUrl

    from dlboard.conftest import MakeBlobStore
    from dlboard.plugins.data_stores._blob_store import BlobArtifactStore
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


class _GatedBlobs(FSBlobs):
    """`FSBlobs` whose writes fail until `gate` exists, as an object store that is down and then back; it marks each try."""

    def __init__(self, root: Path, gate: Path) -> None:
        super().__init__(root)
        self.gate = gate
        self.tried = gate.with_suffix(".tried")

    @override
    def write(self, staged: Path, ref: AnyUrl) -> None:
        self.tried.touch()
        if not self.gate.exists():
            msg = "the object store is down"
            raise ConnectionError(msg)
        super().write(staged, ref)


@pytest.fixture
def run(store: SQLLiteStore, experiment_id: int) -> models.Run:
    return store.create_run(models.NewRun(experiment_id=experiment_id))


@pytest.fixture
def blobs(tmp_path: Path) -> _GatedBlobs:
    return _GatedBlobs(tmp_path / "artifacts", tmp_path / "gate")


def _recorded(store: SQLLiteStore, run: models.Run) -> list[str]:
    return [a.fname for a in store.fetch_artifacts(run.experiment_id, run_ids=None)]


def test_an_upload_outlives_the_process_that_answered_it(
    store: SQLLiteStore, run: models.Run, blobs: _GatedBlobs, tmp_path: Path, make_blob_store: MakeBlobStore
) -> None:
    answered = make_blob_store(blobs, 10)
    upload(answered, run, "a.png")  # answered: the client discards its copy
    answered.dispose()  # and the process dies before the object store took it
    blobs.gate.touch()

    ingest(make_blob_store(blobs, 10), store)  # another process on the same staging directory

    wait_until(lambda: _recorded(store, run) == ["a.png"])
    wait_until(lambda: list((tmp_path / "staging").iterdir()) == [])


def test_a_failed_write_is_tried_again_until_the_object_store_is_back(
    store: SQLLiteStore, run: models.Run, blobs: _GatedBlobs, make_blob_store: MakeBlobStore
) -> None:
    artifact_store = make_blob_store(blobs, 10)
    ingest(artifact_store, store)

    upload(artifact_store, run, "a.png")
    wait_until(blobs.tried.exists)
    blobs.gate.touch()

    wait_until(lambda: _recorded(store, run) == ["a.png"])


def test_a_writer_that_died_is_started_again_with_every_upload_it_had(
    store: SQLLiteStore,
    run: models.Run,
    blobs: _GatedBlobs,
    telemetry: pytest.MonkeyPatch,
    make_blob_store: MakeBlobStore,
) -> None:
    """A killed writer most likely held its queue's read lock: the next one gets a queue of its own."""
    telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "prometheus")
    artifact_store = make_blob_store(blobs, 10)
    ingest(artifact_store, store)
    upload(artifact_store, run, "a.png")
    wait_until(blobs.tried.exists)
    dead = artifact_store._writer
    assert dead is not None
    dead.process.kill()
    dead.process.join()

    with pytest.raises(
        models.ArtifactStoreUnavailableError
    ):  # too soon after it started: not started again yet
        upload(artifact_store, run, "b.png")
    telemetry.setattr(_blob_store, "_RESTART_INTERVAL_SEC", 0.0)
    blobs.gate.touch()

    wait_until(lambda: _recorded(store, run) == ["a.png"])
    assert rejections("writer_down") == 1
    assert (
        scraped_total(serve_metrics().get_data(as_text=True), "dlboard_artifact_writer_restarts_total") == 1
    )


class _StalledStore:
    """Records into a real store only once `going` is set, as a database that stalls: until then, written refs pile up."""

    def __init__(self, store: SQLLiteStore) -> None:
        self._store = store
        self.going = threading.Event()

    def log_artifact_refs(self, artifacts: list[models.Artifact]) -> None:
        self.going.wait()
        self._store.log_artifact_refs(artifacts)

    def fetch_artifacts(self, *args: Any, **kwargs: Any) -> Iterator[models.Artifact]:  # noqa: ANN401
        return self._store.fetch_artifacts(*args, **kwargs)


def test_a_writer_killed_while_the_database_stalls_does_not_wedge_the_one_started_after_it(
    store: SQLLiteStore,
    run: models.Run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_blob_store: MakeBlobStore,
) -> None:
    """
    Results the database has not taken fill the pipe back, and the writer blocks putting one, holding its queue's write lock.

    Killed then, it holds that lock for good: the writer started after it needs a results queue of its own.
    """
    monkeypatch.setattr(_blob_store, "_RESTART_INTERVAL_SEC", 0.0)
    stalled = _StalledStore(store)
    artifact_store = make_blob_store(FSBlobs(tmp_path / "artifacts"), 1000)
    ingest(artifact_store, stalled)
    artifact_store.log_artifacts([file(run, f"{i}.png") for i in range(300)])
    wait_until(
        lambda: not any(p.suffix == "" for p in (tmp_path / "staging").iterdir())
    )  # every blob written
    dead = artifact_store._writer
    assert dead is not None
    dead.process.kill()
    dead.process.join()

    stalled.going.set()

    wait_until(lambda: len(_recorded(store, run)) == 300)


def test_a_writer_that_cannot_be_started_again_at_first_is_started_once_it_can_be(
    store: SQLLiteStore,
    run: models.Run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_blob_store: MakeBlobStore,
) -> None:
    """A writer killed for want of memory may find too little left to start another: the next try must still come."""
    monkeypatch.setattr(_blob_store, "_RESTART_INTERVAL_SEC", 0.0)
    artifact_store = make_blob_store(FSBlobs(tmp_path / "artifacts"), 10)
    ingest(artifact_store, store)
    start, failures = _blob_store._Writer.start, iter([OSError(errno.ENOMEM, "Cannot allocate memory")])

    def start_once_it_can(*args: object) -> object:
        if (failure := next(failures, None)) is not None:
            raise failure
        return start(*args)  # pyrefly: ignore [bad-argument-type]

    monkeypatch.setattr(_blob_store._Writer, "start", staticmethod(start_once_it_can))
    dead = artifact_store._writer
    assert dead is not None
    dead.process.kill()
    dead.process.join()

    wait_until(lambda: _accepts(artifact_store, run, "a.png"))

    wait_until(lambda: _recorded(store, run) == ["a.png"])


def _accepts(artifact_store: BlobArtifactStore, run: models.Run, name: str) -> bool:
    try:
        upload(artifact_store, run, name)
    except models.ArtifactStoreUnavailableError:
        return False
    return True


def test_an_upload_recorded_by_a_process_that_died_before_finishing_it_is_not_recorded_again(
    store: SQLLiteStore, run: models.Run, tmp_path: Path, make_blob_store: MakeBlobStore
) -> None:
    blobs = FSBlobs(tmp_path / "artifacts")
    new_artifact = models.NewArtifact(
        key="img", fname="a.png", run_id=run.id, experiment_id=run.experiment_id, step=0
    )
    ref = blobs.ref_for(blob_key(run.experiment_id, run.id, "img", 0, "a.png"))
    dying = UploadJournal(tmp_path / "staging")
    dying.accept(new_artifact, ref, FileStorage(io.BytesIO(b"data"), filename="a.png"))
    store.log_artifact_refs([models.Artifact.model_validate(new_artifact.model_dump() | {"ref": str(ref)})])
    dying.let_go()  # it died between recording the ref and finishing the upload

    ingest(make_blob_store(blobs, 10), store)

    wait_until(lambda: list((tmp_path / "staging").iterdir()) == [])
    assert _recorded(store, run) == ["a.png"]


def test_uploads_are_turned_away_while_a_process_holds_its_limit_unwritten(
    run: models.Run, blobs: _GatedBlobs, telemetry: pytest.MonkeyPatch, make_blob_store: MakeBlobStore
) -> None:
    """With the object store down, uploads are turned away rather than piling up on the server's disk."""
    telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "prometheus")
    telemetry.setattr(_blob_store, "_ENQUEUE_TIMEOUT_SEC", 0.1)
    artifact_store = make_blob_store(blobs, 1)
    upload(artifact_store, run, "a.png")

    with pytest.raises(models.ArtifactStoreUnavailableError):
        upload(artifact_store, run, "b.png")

    assert rejections("queue_full") == 1
