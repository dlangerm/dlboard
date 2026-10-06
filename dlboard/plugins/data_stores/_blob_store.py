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
import tempfile
from enum import Enum, auto
from pathlib import Path, PurePosixPath
from threading import Thread
from typing import TYPE_CHECKING, Protocol, Self, override
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
    input_q: Queue[tuple[NewArtifact, AnyUrl, Path]],
    return_q: Queue[Artifact],
) -> None:
    # This runs in its own `SPAWN_CONTEXT`-spawned process (see `BlobArtifactStore.__init__`), a
    # fresh interpreter with none of the parent's structlog setup -- without this, a write failure
    # here (an S3 signing error, say) would log with structlog's own default traceback renderer,
    # which prints every frame's locals, including whatever credentials `backend.write` was holding.
    configure_logging()
    while True:
        try:
            a, ref, staged = input_q.get()
            backend.write(staged, ref)
            artifact = models.Artifact.model_validate(a.model_dump() | {"ref": str(ref)})
            if return_q.full():
                _log.warning("Return queue is full, runtime will be impacted")
            return_q.put(artifact)
        except KeyboardInterrupt:
            return
        except Exception:  # noqa: BLE001
            _log.exception("Failed to save artifacts")


class BlobArtifactStore(models.ArtifactStore[BlobBackend, int]):
    """An `ArtifactStore` for any `BlobBackend` -- staging, batched writes, and ref-ingest are shared."""

    def __init__(self, backend: BlobBackend, store_q_size: int) -> None:
        """Initialize the store, starting its background blob-writing worker process."""
        self._backend = backend
        self._store_q: Queue[tuple[NewArtifact, AnyUrl, Path]] = SPAWN_CONTEXT.Queue(store_q_size)
        self._saved_artifact_q: Queue[Artifact] = SPAWN_CONTEXT.Queue(store_q_size)
        self._store_artifact_proc = SPAWN_CONTEXT.Process(
            target=_write_blobs,
            args=(backend, self._store_q, self._saved_artifact_q),
            daemon=True,
        )
        self._file_staging_dir = Path(tempfile.gettempdir())
        self._store_artifact_proc.start()

    def dispose(self) -> None:
        """
        Stop the write-worker process and close its queues, for a store that's going away.

        A real deployment's `Dash` app never calls this -- its store lives as long as the server
        does. A test building many short-lived stores in one process does need it: left running,
        each one's worker process and queues (and the ref-ingest thread reading from them, which
        this also stops -- `ship_batches` returns once its queue is closed) pile up for the rest
        of the session instead of releasing their OS-level resources (file descriptors, semaphores).
        """
        self._store_artifact_proc.terminate()
        self._store_artifact_proc.join()
        self._store_q.close()
        self._saved_artifact_q.close()

    def ingest_stored_artifacts(self, app: Dash) -> None:
        """Forever record the refs of blobs the write worker has finished writing into `app`'s data store."""
        ship_batches(
            self._saved_artifact_q,
            functools.partial(self._record_refs, wait_for_data_store(app)),
            BatchParams(
                flush_size=10, wait_sec=1, ready=SPAWN_CONTEXT.Event(), flushed=SPAWN_CONTEXT.Event()
            ),
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
                self._store_q.put((a, ref, staging_path))
        except Exception:
            _log.exception("failed to save artifact batch, reverting to allow client retry")
            raise

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


def plug_blob_store(app: Dash, backend: BlobBackend, store_q_size: int) -> None:
    """Set `app`'s artifact store to a `BlobArtifactStore` over `backend`, and start ingesting its refs."""
    store = BlobArtifactStore.get_or_create(backend, store_q_size)
    set_artifact_store(app, store)
    Thread(target=store.ingest_stored_artifacts, args=(app,), daemon=True, name="artifact-ref-ingest").start()
