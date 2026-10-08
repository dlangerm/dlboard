"""
Shared artifact-blob pipeline: staging, the async write, and the ref-ingest loop.

Every `ArtifactStore` dlboard ships (filesystem, S3, ...) stores a blob somewhere a client's
upload eventually lands and a browser can later fetch it -- staging the upload, batching writes
off to a worker process, and reporting finished refs back to the `DataStore` is identical
regardless of *where* that somewhere is. `BlobBackend` is the one seam that differs per backend;
`BlobArtifactStore` is a complete, generic `ArtifactStore` built on top of it. A concrete backend
(`filesystem.py`'s `FSBlobs`, `s3.py`'s `S3Blobs`) only has to implement `BlobBackend`.
"""

from __future__ import annotations

import functools
import hashlib
import queue
import signal
import tempfile
import time
from enum import Enum, auto
from multiprocessing import parent_process, util
from pathlib import Path, PurePosixPath
from threading import Thread
from typing import TYPE_CHECKING, Final, Protocol, Self, override
from uuid import uuid4

from flask import abort
from pydantic import AnyUrl
from structlog.stdlib import get_logger

from dlboard import models
from dlboard._batching import BatchParams, ship_batches
from dlboard._mp_context import SPAWN_CONTEXT
from dlboard.serve import set_artifact_store, wait_for_data_store
from dlboard.serve._logging import configure_logging

if TYPE_CHECKING:
    from collections.abc import Iterable
    from multiprocessing import Queue

    from dash import Dash
    from flask import Response
    from werkzeug.datastructures import FileStorage

    from dlboard.models import DataStore
    from dlboard.models._artifact import Artifact, NewArtifact


_log = get_logger(__name__)

_ENQUEUE_TIMEOUT_SEC: Final = 30
"""How long an upload waits for room in a full write queue before it's turned away with a 503."""

_DRAIN_TIMEOUT_SEC: Final = 20
"""How long shutdown waits for queued blobs to be written and their refs recorded. Kept under
Granian's `DLBOARD_WORKERS_KILL_TIMEOUT_S` default so the drain finishes before the worker is killed."""

_STALE_STAGING_SEC: Final = 24 * 60 * 60
"""A staged upload this old belongs to a process that died; every worker shares one staging dir, so
only files older than any live upload could be are swept."""

_FINALIZER_PRIORITY: Final = 100
"""Above the priority (10) `multiprocessing.Queue` closes its own feeder thread at: this drain still
has to put its stop signals through those queues, so it must run before they shut."""

_WRITER_POLL_SEC: Final = 1.0
"""How often an idle write worker checks that the server process that spawned it is still alive."""


class RefAccess(Enum):
    """What a backend will let dlboard do with a ref, whether it just wrote that blob or not."""

    OWNED = auto()
    """Inside the store's own space: linkable, downloadable, and (on purge) deletable."""

    READ_ONLY = auto()
    """Outside the store's own space but explicitly allowlisted: linkable and downloadable, but
    never deleted -- dlboard didn't put it there and has no business removing it."""


class BlobBackend(Protocol):
    """
    One blob store's actual I/O; everything else in this module is backend-agnostic.

    An implementation holds only picklable configuration (settings, a root path) -- never a live
    client or connection -- because `write` runs inside a spawned worker process and gets there by
    being pickled along with it. It opens whatever connection it needs itself, on the worker side.
    """

    def ref_for(self, key: PurePosixPath) -> AnyUrl:
        """The ref a freshly written blob at `key` will get."""
        ...

    def access(self, ref: AnyUrl) -> RefAccess | None:
        """Whether `ref` is servable by this backend at all, and if so, whether dlboard owns it."""
        ...

    def exists(self, ref: AnyUrl) -> bool:
        """Whether a blob is already stored at `ref`."""
        ...

    def write(self, staged: Path, ref: AnyUrl) -> None:
        """Move `staged`'s bytes to `ref`, consuming `staged`."""
        ...

    def download(self, ref: AnyUrl) -> Response:
        """Serve `ref`'s bytes to a browser. Only called once `access(ref)` is not `None`."""
        ...

    def delete(self, ref: AnyUrl) -> None:
        """Permanently delete the blob at `ref`. Idempotent: a blob already gone is not an error."""
        ...


def blob_key(experiment_id: int, run_id: int, key: str, step: int | None, fname: str) -> PurePosixPath:
    """A stable, collision-resistant key for one artifact's blob, unique per (run, key, step, fname)."""
    fname_hsh = hashlib.shake_256(fname.encode(), usedforsecurity=False).hexdigest(16)
    key_hsh = hashlib.shake_256(key.encode(), usedforsecurity=False).hexdigest(16)
    _step = f"{step}" if step is not None else "_anon_"
    return PurePosixPath(str(experiment_id), str(run_id), key_hsh, _step, f"{fname_hsh}{Path(fname).suffix}")


def _write_blobs(
    backend: BlobBackend,
    input_q: Queue[tuple[NewArtifact, AnyUrl, Path] | None],
    return_q: Queue[Artifact | None],
) -> None:
    # This runs in its own `SPAWN_CONTEXT`-spawned process (see `BlobArtifactStore.__init__`), a
    # fresh interpreter with none of the parent's structlog setup -- without this, a write failure
    # here (an S3 signing error, say) would log with structlog's own default traceback renderer,
    # which prints every frame's locals, including whatever credentials `backend.write` was holding.
    configure_logging()
    # A Ctrl-C reaches this process too (same process group); it must keep writing until the server
    # process tells it to stop with a `None`, or the blobs still queued would be lost on shutdown.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    parent = parent_process()
    while True:
        try:
            item = input_q.get(timeout=_WRITER_POLL_SEC)
        except queue.Empty:
            if parent is not None and not parent.is_alive():
                return  # the server died without asking us to stop; nothing will record our refs
            continue
        if item is None:
            return
        a, ref, staged = item
        try:
            backend.write(staged, ref)
            artifact = models.Artifact.model_validate(a.model_dump() | {"ref": str(ref)})
            if return_q.full():
                _log.warning("Return queue is full, runtime will be impacted")
            return_q.put(artifact)
        except Exception:  # noqa: BLE001
            _log.exception("Failed to save artifacts")
            staged.unlink(missing_ok=True)


class BlobArtifactStore(models.ArtifactStore[BlobBackend, int]):
    """An `ArtifactStore` for any `BlobBackend` -- staging, batched writes, and ref-ingest are shared."""

    def __init__(self, backend: BlobBackend, store_q_size: int, staging_dir: Path | None = None) -> None:
        """
        Initialize the store, starting its background blob-writing worker process.

        Uploads are staged in `staging_dir` before being written to `backend`. Pass a directory on the
        same filesystem as the backend's storage so the final move is a rename, and a crash leaves
        its leftovers somewhere the operator already provisions and monitors. Defaults to the system temp dir.
        """
        self._backend = backend
        self._store_q: Queue[tuple[NewArtifact, AnyUrl, Path] | None] = SPAWN_CONTEXT.Queue(store_q_size)
        self._saved_artifact_q: Queue[Artifact | None] = SPAWN_CONTEXT.Queue(store_q_size)
        self._store_artifact_proc = SPAWN_CONTEXT.Process(
            target=_write_blobs,
            args=(backend, self._store_q, self._saved_artifact_q),
            daemon=True,
        )
        self._file_staging_dir = staging_dir or Path(tempfile.gettempdir())
        self._file_staging_dir.mkdir(parents=True, exist_ok=True)
        self._sweep_stale_staging()
        self._ingest_flushed = SPAWN_CONTEXT.Event()
        self._ingest_ready = SPAWN_CONTEXT.Event()
        self._store_artifact_proc.start()
        # Not `atexit`: a Granian worker is a `multiprocessing` child, which exits through
        # `util._exit_function` (running only these finalizers) and never reaches `atexit` handlers.
        # Priority >= 0 also runs it before multiprocessing terminates its daemonic children (our writer).
        self._finalizer = util.Finalize(None, self.close, exitpriority=_FINALIZER_PRIORITY)

    def _sweep_stale_staging(self) -> None:
        """Delete staged uploads left behind by a process that died before writing them."""
        cutoff = time.time() - _STALE_STAGING_SEC
        for leftover in self._file_staging_dir.iterdir():
            if leftover.is_file() and leftover.stat().st_mtime < cutoff:
                leftover.unlink(missing_ok=True)

    def close(self, timeout: float = _DRAIN_TIMEOUT_SEC) -> None:
        """
        Gracefully stop: write every queued blob, record their refs, then stop the write worker.

        Runs when the process exits (and is safe to call early or twice). Whatever doesn't finish within
        `timeout` is abandoned with a warning -- the worker is terminated, and the client's retry of
        the upload that never got a ref recorded is what recovers it.
        """
        self._finalizer.cancel()
        proc = self._store_artifact_proc
        if not proc.is_alive():
            return
        deadline = time.monotonic() + timeout
        try:
            self._store_q.put(None, timeout=max(deadline - time.monotonic(), 0))
        except queue.Full:
            _log.warning("Shutting down with the blob write queue still full")
        proc.join(max(deadline - time.monotonic(), 0))
        if proc.is_alive():
            _log.warning("Blob writer did not finish in %ss, abandoning its remaining queue", timeout)
            proc.terminate()
            proc.join()
        elif self._ingest_ready.is_set():
            self._saved_artifact_q.put(
                None
            )  # a flush request: acked once every ref queued before it is recorded
            if not self._ingest_flushed.wait(max(deadline - time.monotonic(), 0)):
                _log.warning("Some written artifacts' refs were not recorded before shutdown")

    def dispose(self) -> None:
        """
        Stop the write-worker process and close its queues, for a store that's going away.

        A real deployment's `Dash` app never calls this -- its store lives as long as the server
        does. A test building many short-lived stores in one process does need it: left running,
        each one's worker process and queues (and the ref-ingest thread reading from them, which
        this also stops -- `ship_batches` returns once its queue is closed) pile up for the rest
        of the session instead of releasing their OS-level resources (file descriptors, semaphores).
        """
        self._finalizer.cancel()
        self._store_artifact_proc.terminate()
        self._store_artifact_proc.join()
        self._store_q.close()
        self._saved_artifact_q.close()

    def ingest_stored_artifacts(self, app: Dash) -> None:
        """Forever record the refs of blobs the write worker has finished writing into `app`'s data store."""
        ship_batches(
            self._saved_artifact_q,
            functools.partial(self._record_refs, wait_for_data_store(app)),
            BatchParams(flush_size=10, wait_sec=1, ready=self._ingest_ready, flushed=self._ingest_flushed),
            # `_record_refs` already drops (and cleans up after) anything the store rejects outright,
            # so whatever still escapes it -- a locked database, say -- is worth retrying.
            is_permanent=lambda _exc: False,
        )

    def _record_refs(self, store: DataStore[...], artifacts: list[Artifact]) -> None:
        """
        Record `artifacts`' refs; one the store rejects outright is dropped alone, its blob deleted.

        `log_artifact_refs` raises `ValueError` for input it will never accept (e.g. a run deleted
        while its upload was in flight) and checks the whole batch before writing any of it -- so on
        one, retry each artifact alone: only the rejected ones are dropped, and their already-written
        blobs deleted rather than orphaned with nothing referencing them.
        """
        try:
            store.log_artifact_refs(artifacts)
        except ValueError:
            if len(artifacts) > 1:
                for artifact in artifacts:
                    self._record_refs(store, [artifact])
                return
            _log.exception("The data store rejected artifact %s, deleting its blob", artifacts[0].ref)
            self.delete_artifact(AnyUrl(artifacts[0].ref))

    @classmethod
    def get_or_create(cls, backend: BlobBackend, store_q_size: int) -> Self:
        """Create a new store on top of `backend`."""
        return cls(backend, store_q_size)

    def log_artifacts(self, artifacts: Iterable[tuple[NewArtifact, FileStorage]]) -> None:
        """Stage each uploaded file, then queue it for the write worker."""
        if not self._store_artifact_proc.is_alive():
            msg = "the artifact write worker is not running"
            raise models.ArtifactStoreUnavailableError(msg)
        staging_path: Path | None = None
        try:
            for a, file in artifacts:
                ref = self._backend.ref_for(blob_key(a.experiment_id, a.run_id, a.key, a.step, a.fname))
                if self._backend.exists(ref):
                    msg = f"duplicate key fname uploaded! {ref}"
                    raise ValueError(msg)  # noqa: TRY301

                if self._store_q.full():
                    _log.warning("Store queue is full, runtime will be impacted")
                staging_path = self._file_staging_dir / str(uuid4())
                file.save(staging_path, buffer_size=int(1e6))
                self._store_q.put((a, ref, staging_path), timeout=_ENQUEUE_TIMEOUT_SEC)
                staging_path = None  # the write worker owns it now
        except queue.Full as exc:
            msg = "the artifact write queue is full"
            raise models.ArtifactStoreUnavailableError(msg) from exc
        except Exception:
            _log.exception("failed to save artifact batch, reverting to allow client retry")
            raise
        finally:
            if staging_path is not None:
                staging_path.unlink(missing_ok=True)

    @override
    def link_artifacts(self, links: Iterable[tuple[NewArtifact, AnyUrl]]) -> list[Artifact]:
        links = list(links)
        unservable = [
            ref for _, ref in links if self._backend.access(ref) is None or not self._backend.exists(ref)
        ]
        if unservable:
            msg = f"refusing to link {len(unservable)} ref(s) this store can't serve: {unservable}"
            raise models.UnservableArtifactRefError(msg)
        return [models.Artifact.model_validate(a.model_dump() | {"ref": str(ref)}) for a, ref in links]

    @override
    def download_artifact(self, ref: AnyUrl) -> Response:
        if self._backend.access(ref) is None:
            _log.warning("Refusing to serve a ref this backend doesn't recognize: %s", ref)
            abort(404)
        return self._backend.download(ref)

    @override
    def delete_artifact(self, ref: AnyUrl) -> None:
        match self._backend.access(ref):
            case RefAccess.OWNED:
                _log.info("Deleting artifact blob at %s", ref)
                self._backend.delete(ref)
            case RefAccess.READ_ONLY:
                _log.info("Not deleting %s: outside this store's own space", ref)
            case None:
                _log.warning("Refusing to delete a ref this backend doesn't recognize: %s", ref)


def plug_blob_store(
    app: Dash, backend: BlobBackend, store_q_size: int, staging_dir: Path | None = None
) -> None:
    """Set `app`'s artifact store to a `BlobArtifactStore` over `backend`, and start ingesting its refs."""
    store = BlobArtifactStore(backend, store_q_size, staging_dir)
    set_artifact_store(app, store)
    Thread(target=store.ingest_stored_artifacts, args=(app,), daemon=True, name="artifact-ref-ingest").start()
