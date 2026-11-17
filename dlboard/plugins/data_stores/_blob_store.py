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
import heapq
import itertools
import math
import queue
import signal
import time
from dataclasses import dataclass
from enum import Enum, StrEnum, auto
from multiprocessing import parent_process, util
from pathlib import Path, PurePosixPath
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, Final, Protocol, Self, override

from flask import abort
from pydantic import AnyUrl
from structlog.stdlib import get_logger

from dlboard import models
from dlboard._batching import BatchParams, Failure, ship_batches
from dlboard._mp_context import SPAWN_CONTEXT
from dlboard._telemetry import Attributes, attribute
from dlboard._telemetry.traces import SpanKind, TraceKey, carrier, flush, span
from dlboard.plugins.data_stores._upload_journal import Held, UploadJournal
from dlboard.serve import set_artifact_store, wait_for_data_store
from dlboard.serve._custodian import in_custodian
from dlboard.serve._logging import configure_logging
from dlboard.serve.telemetry import (
    AttributeKey,
    Counter,
    Histogram,
    NoAttributes,
    Outcome,
    OutcomeAttributes,
    Unit,
    instrument_libraries,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from multiprocessing import Queue
    from multiprocessing.process import BaseProcess

    from dash import Dash
    from flask import Response
    from werkzeug.datastructures import FileStorage

    from dlboard._telemetry.traces import ActiveSpan, Carrier
    from dlboard.models import DataStore
    from dlboard.models._artifact import Artifact, NewArtifact


_log = get_logger(__name__)


def _waited_since(stamp: float) -> float:
    """
    How long since `stamp`, a `time.monotonic()` the process that queued the item took: never negative.

    The writer is another process on this host. Its monotonic clock is the same one on the platforms dlboard runs on, but
    Python does not promise that across processes, so a stamp that reads as later than now is clamped to zero.
    """
    return max(time.monotonic() - stamp, 0.0)


@dataclass(frozen=True, slots=True)
class _Upload:
    """What the server hands the writer: the upload, where it goes, where it is staged, and the request's trace."""

    artifact: NewArtifact
    ref: AnyUrl
    staged: Path
    trace: Carrier
    accepted_at: float
    """When the server queued it, by `time.monotonic()`, read by the writer through `_waited_since`."""


@dataclass(frozen=True, slots=True)
class _Saved:
    """What the writer hands back to be recorded: the upload's journal name, the artifact, and the write's trace."""

    name: str
    artifact: Artifact
    trace: Carrier
    written_at: float
    """When the writer finished it, by `time.monotonic()`, as `_Upload.accepted_at`."""


@dataclass(frozen=True, slots=True)
class _Gone:
    """An upload whose staged bytes are gone though its blob was never written: there is nothing left to write."""

    name: str


class _Rejection(StrEnum):
    QUEUE_FULL = "queue_full"
    WRITER_DOWN = "writer_down"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class _RejectionAttributes(Attributes):
    reason: _Rejection = attribute(AttributeKey.REJECTION_REASON)


_REJECTIONS = Counter[_RejectionAttributes](
    "dlboard.artifact.write_queue.rejections",
    Unit.UPLOADS,
    "Uploads turned away with a 503 because the blob writer could not take them.",
)
_RESTARTS = Counter[NoAttributes](
    "dlboard.artifact.writer.restarts", Unit.WORKERS, "Blob writers started again after the one before died."
)


@dataclass(frozen=True, slots=True)
class _WriteTrace(Attributes):
    experiment_id: int = attribute(TraceKey.EXPERIMENT_ID)
    run_id: int = attribute(TraceKey.RUN_ID)
    queue_wait: float = attribute(TraceKey.QUEUE_WAIT)


@dataclass(frozen=True, slots=True)
class _IngestTrace(Attributes):
    items: int = attribute(TraceKey.ITEMS)
    queue_wait: float = attribute(TraceKey.QUEUE_WAIT)
    """The longest any of the batch waited."""


# With `_BLOB_WRITES`, these cover an upload's whole way from the request that staged it to its ref being recorded.
_STAGING = Histogram[NoAttributes](
    "dlboard.artifact.stage.duration", Unit.SECONDS, "How long a request took to stage one uploaded file."
)
_WRITE_QUEUE_WAIT = Histogram[NoAttributes](
    "dlboard.artifact.write_queue.wait",
    Unit.SECONDS,
    "How long a staged upload waited for the writer to take it.",
)
_BLOB_WRITES = Histogram[OutcomeAttributes](
    "dlboard.artifact.blob.write.duration", Unit.SECONDS, "How long the writer took to store one artifact."
)
_INGEST_QUEUE_WAIT = Histogram[NoAttributes](
    "dlboard.artifact.ingest_queue.wait",
    Unit.SECONDS,
    "How long a written artifact waited for the batch that recorded its ref, the batching itself included.",
)
_INGESTS = Histogram[OutcomeAttributes](
    "dlboard.artifact.ingest.duration",
    Unit.SECONDS,
    "How long recording one batch of written artifacts' refs took.",
)

_ENQUEUE_TIMEOUT_SEC: Final = 30
"""How long an upload waits for its worker to hold fewer than its limit of unfinished uploads before it's turned away with a 503."""

_DRAIN_TIMEOUT_SEC: Final = 20
"""How long shutdown waits for queued blobs to be written and their refs recorded. Kept under
Granian's `DLBOARD_WORKERS_KILL_TIMEOUT_S` default so the drain finishes before the worker is killed."""

_TEND_SEC: Final = 1.0
"""How often a worker checks that its writer is alive."""

_ADOPT_EVERY_SEC: Final = 30.0
"""How often a worker looks for uploads a process that died left unfinished, after the first look as it starts."""

_SWEEP_EVERY_SEC: Final = 60 * 60.0
"""How often a worker sweeps the staging directory of what requests that died before answering left, after the sweep as
the store starts: a server that runs for months must not wait for a restart to drop them."""

_RETRY_BACKOFF_SEC: Final = (1.0, 300.0)
"""How long the writer waits to try a failed write again: doubling from the first, never past the second."""

_FINALIZER_PRIORITY: Final = 100
"""Above the priority (10) `multiprocessing.Queue` closes its own feeder thread at: this drain still
has to put its stop signals through those queues, so it must run before they shut."""

_WRITER_POLL_SEC: Final = 1.0
"""How often an idle write worker checks that the server process that spawned it is still alive."""

_RESTART_INTERVAL_SEC: Final = 10.0
"""The least time between two starts of a write worker: one that dies as it starts is not started again for every upload."""


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
        """
        Store `staged`'s bytes at `ref`, replacing whatever is there, and consume `staged` once they are.

        A write that fails must leave `staged` where it is: it is the only copy, and the write is tried again.
        """
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


def _attempt(backend: BlobBackend, upload: _Upload) -> _Saved | _Gone | None:
    """
    Write `upload`'s blob; `None` if that failed this time, to be tried again.

    Done twice, the second finds its bytes gone and the blob written: the first's result may not have reached the
    server, or a writer that died may have written it before the server gave the upload to this one.
    """
    a = upload.artifact
    started = time.perf_counter()
    outcome = Outcome.FAILURE
    try:
        # The request that uploaded this is long gone: the trace crossed the queue in the item.
        with span(
            "dlboard.blob.write",
            SpanKind.CONSUMER,
            _WriteTrace(
                experiment_id=a.experiment_id, run_id=a.run_id, queue_wait=_waited_since(upload.accepted_at)
            ),
            parent=upload.trace,
        ):
            if upload.staged.exists():
                backend.write(upload.staged, upload.ref)
            elif not backend.exists(upload.ref):
                return _Gone(upload.staged.name)
            write_trace = carrier()
        outcome = Outcome.SUCCESS
    except Exception:  # noqa: BLE001 -- nothing may kill the writer, and every upload is tried again
        _log.exception("Failed to write an artifact's blob; trying again later", ref=str(upload.ref))
        return None
    finally:
        _BLOB_WRITES.record(time.perf_counter() - started, OutcomeAttributes(outcome=outcome))
    artifact = models.Artifact.model_validate(a.model_dump() | {"ref": str(upload.ref)})
    return _Saved(upload.staged.name, artifact, write_trace, time.monotonic())


def _write_blobs(
    backend: BlobBackend,
    input_q: Queue[_Upload | None],
    return_q: Queue[_Saved | _Gone | None],
) -> None:
    # This runs in its own `SPAWN_CONTEXT`-spawned process (see `BlobArtifactStore.__init__`), a
    # fresh interpreter with none of the parent's structlog setup -- without this, a write failure
    # here (an S3 signing error, say) would log with structlog's own default traceback renderer,
    # which prints every frame's locals, including whatever credentials `backend.write` was holding.
    configure_logging()
    # Likewise a fresh interpreter for tracing: this process is dlboard's alone, so it patches boto here.
    instrument_libraries()
    # A Ctrl-C reaches this process too (same process group); it must keep writing until the server
    # process tells it to stop with a `None`, or the blobs still queued would be lost on shutdown.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    parent = parent_process()
    # A failed write waits its turn here, by when it is due, so that one the backend keeps refusing holds up no other.
    retries: list[tuple[float, int, int, _Upload]] = []  # (due, tiebreak, attempts, upload)
    tiebreak = itertools.count()
    while True:
        due: list[tuple[int, _Upload]] = []
        wait = _WRITER_POLL_SEC if not retries else retries[0][0] - time.monotonic()
        try:
            item = input_q.get(timeout=min(max(wait, 0.0), _WRITER_POLL_SEC))
        except queue.Empty:
            if parent is not None and not parent.is_alive():
                flush()
                return  # the server died without asking us to stop; what it held waits on disk for another
        else:
            if item is None:
                flush()
                return  # what still waits to be tried again stays on disk, for the next start
            _WRITE_QUEUE_WAIT.record(_waited_since(item.accepted_at))
            due.append((0, item))
        while retries and retries[0][0] <= time.monotonic():
            _, _, attempts, upload = heapq.heappop(retries)
            due.append((attempts, upload))
        for attempts, upload in due:
            match _attempt(backend, upload):
                case None:
                    first, cap = _RETRY_BACKOFF_SEC
                    again = time.monotonic() + min(first * 2.0**attempts, cap)
                    heapq.heappush(retries, (again, next(tiebreak), attempts + 1, upload))
                case result:
                    if return_q.full():
                        _log.warning("Return queue is full, runtime will be impacted")
                    return_q.put(result)


@dataclass(frozen=True, slots=True)
class _Writer:
    """
    A write worker: its process, the queue only it reads, and the one only it puts to.

    They are replaced together, since a worker that is killed (out of memory, say) can leave either queue's lock held for
    good. An idle worker waits inside `queue.get`, holding that queue's read lock. One whose results the database has not
    taken yet fills `results`' pipe and blocks putting the next, holding that queue's write lock.
    """

    process: BaseProcess
    queue: Queue[_Upload | None]
    """Unbounded: what this process holds is bounded instead (`UploadJournal.wait_for_room`), so a put never waits."""
    results: Queue[_Saved | _Gone | None]

    @classmethod
    def start(cls, backend: BlobBackend, results: int) -> _Writer:
        """Start a worker writing to `backend` on fresh queues, with room for `results` of what it wrote (0: no limit)."""
        uploads: Queue[_Upload | None] = SPAWN_CONTEXT.Queue()
        saved: Queue[_Saved | _Gone | None] = SPAWN_CONTEXT.Queue(results)
        process = SPAWN_CONTEXT.Process(target=_write_blobs, args=(backend, uploads, saved), daemon=True)
        process.start()
        return cls(process, uploads, saved)

    def send(self, held: Held, trace: Carrier) -> None:
        """Give the worker `held` to write."""
        self.queue.put(_Upload(held.artifact, held.ref, held.staged, trace, time.monotonic()))


class _Results:
    """
    The current writer's results, for the ingest loop to read: it moves to a writer started again at its next read.

    The dead writer's queue is then dropped. Uploads it had written but whose results never arrived are still held, and
    are given to the new writer, which finds each one's blob there and hands back its result again.
    """

    def __init__(self, writer: _Writer) -> None:
        self.writer = writer

    def get(self, block: bool = True, timeout: float | None = None) -> _Saved | _Gone | None:  # noqa: FBT001, FBT002
        return self.writer.results.get(block, timeout)


def _recorded_refs(store: DataStore[...], artifacts: list[Artifact]) -> set[str]:
    """
    Which of `artifacts`' refs are recorded already.

    By an upload a client sent again after its first try had landed, or by a process that died after recording one but
    before it could finish it. One indexed read an experiment, and a batch is almost always one run's.
    """
    wanted, steps = {a.ref for a in artifacts}, {a.step for a in artifacts}
    return {
        found.ref
        for experiment_id in {a.experiment_id for a in artifacts}
        for found in store.fetch_artifacts(
            experiment_id,
            keys=frozenset(a.key for a in artifacts),
            run_ids=frozenset(a.run_id for a in artifacts),
            steps=None if None in steps else frozenset(step for step in steps if step is not None),
        )
        if found.ref in wanted
    }


class BlobArtifactStore(models.ArtifactStore[BlobBackend, int, Path]):
    """
    An `ArtifactStore` for any `BlobBackend` -- staging, batched writes, and ref-ingest are shared.

    An upload is answered once it is in the journal (`UploadJournal`), and from then on is the server's to finish however its
    processes die: the writer retries a write that fails, a writer that dies is started again and given everything this
    process holds, and an upload whose process died is taken over by another sharing the staging directory.
    """

    def __init__(
        self, backend: BlobBackend, store_q_size: int, staging_dir: Path, *, writer: bool = True
    ) -> None:
        """
        Initialize the store, starting its background blob-writing worker process (unless `writer` is off).

        Without a writer the store takes no upload (`log_artifacts` says it is unavailable) but serves and deletes blobs:
        the custodian process's, which runs background tasks and serves no request.

        Uploads are kept in `staging_dir` until written to `backend`: they are the only copy, so it must be on storage that
        outlives the server (never a tmpfs), and this store's alone, since a store takes over every upload it finds there
        that no live process holds. Pass a directory on the same filesystem as the backend's storage so the final move is a
        rename. `store_q_size` is how many uploads one process holds unfinished before it turns more away (0: no limit).
        """
        self._backend = backend
        self._limit = store_q_size or math.inf
        self._results_size = store_q_size
        self._journal = UploadJournal(staging_dir)
        self._journal.sweep()
        self._ingest_flushed = SPAWN_CONTEXT.Event()
        self._ingest_ready = SPAWN_CONTEXT.Event()
        self._stopping = Event()
        self._writer_lock = Lock()
        self._writer = _Writer.start(backend, store_q_size) if writer else None
        """`None` once the store takes no more uploads: it never had a writer, or it was closed."""
        self._results = None if self._writer is None else _Results(self._writer)
        """What the ingest loop reads: the latest writer's results, still after the store is closed, so it can drain them."""
        if self._writer is not None:
            Thread(target=self._tend, daemon=True, name="artifact-writer-tender").start()
        # Not `atexit`: a Granian worker is a `multiprocessing` child, which exits through
        # `util._exit_function` (running only these finalizers) and never reaches `atexit` handlers.
        # Priority >= 0 also runs it before multiprocessing terminates its daemonic children (our writer).
        self._finalizer = util.Finalize(None, self.close, exitpriority=_FINALIZER_PRIORITY)

    def _tend(self) -> None:
        """
        Keep every upload this process holds on its way, until the store closes.

        A writer that died is started again (no sooner than `_RESTART_INTERVAL_SEC` after the last start, or try at one, so
        that one dying as it starts is not started over and over) and given every upload this process holds: what the dead
        one had queued died with its queue. Every `_ADOPT_EVERY_SEC`, this process takes over the uploads of any process
        that died, as many as it has room for; every `_SWEEP_EVERY_SEC`, it sweeps the staging directory. Anything that
        fails is tried again: nothing else would ever restart a writer.
        """
        started_at, adopted_at, swept_at = time.monotonic(), -math.inf, time.monotonic()
        while not self._stopping.is_set():
            try:
                with self._writer_lock:
                    writer = self._writer
                    if writer is None:
                        return
                    if (
                        not writer.process.is_alive()
                        and time.monotonic() - started_at >= _RESTART_INTERVAL_SEC
                    ):
                        started_at = time.monotonic()
                        writer = self._restart(writer)
                    if time.monotonic() - adopted_at >= _ADOPT_EVERY_SEC:
                        adopted_at = time.monotonic()
                        adopted = self._journal.adopt(self._limit)
                        if adopted:
                            _log.warning("Taking over uploads a process died holding", count=len(adopted))
                        for held in adopted:
                            writer.send(held, {})
                if time.monotonic() - swept_at >= _SWEEP_EVERY_SEC:
                    swept_at = time.monotonic()
                    self._journal.sweep()
            except Exception:  # noqa: BLE001 -- the tender must outlive whatever it tends
                _log.exception("Tending the blob writer failed; trying again")
            self._stopping.wait(_TEND_SEC)

    def _restart(self, dead: _Writer) -> _Writer:
        """Start a writer in place of `dead`, on queues of its own, and give it every upload this process holds."""
        dead.process.join()
        _log.error("The blob writer died; starting another", exitcode=dead.process.exitcode)
        dead.queue.cancel_join_thread()  # nothing reads it any more: exiting must not wait to flush it
        dead.queue.close()
        writer = self._writer = _Writer.start(self._backend, self._results_size)
        if self._results is not None:
            self._results.writer = writer
        _RESTARTS.add(1)
        for held in self._journal.held():
            writer.send(held, {})
        return writer

    def close(self, timeout: float = _DRAIN_TIMEOUT_SEC) -> None:
        """
        Gracefully stop: write every queued blob, record their refs, then stop the write worker.

        Runs when the process exits (and is safe to call early or twice), and the store takes no upload after it.
        Whatever doesn't finish within `timeout`, or is waiting to be tried again, stays in the journal for the next
        process to start on this staging directory.
        """
        self._finalizer.cancel()
        self._stopping.set()
        with self._writer_lock:
            writer, self._writer = self._writer, None
        try:
            if writer is None or not writer.process.is_alive():
                return
            proc = writer.process
            deadline = time.monotonic() + timeout
            writer.queue.put(None)
            proc.join(max(deadline - time.monotonic(), 0))
            if proc.is_alive():
                _log.warning("Blob writer did not finish in %ss; leaving the rest in the journal", timeout)
                proc.terminate()
                proc.join()
            elif self._ingest_ready.is_set():
                writer.results.put(None)  # a flush request: acked once every ref queued before it is recorded
                if not self._ingest_flushed.wait(max(deadline - time.monotonic(), 0)):
                    _log.warning("Some written artifacts' refs were not recorded before shutdown")
        finally:
            self._journal.let_go()

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
        self._stopping.set()
        with self._writer_lock:
            writer, self._writer = self._writer, None
        if writer is not None:
            writer.process.terminate()
            writer.process.join()
            writer.queue.close()
        if self._results is not None:
            self._results.writer.results.close()
        self._journal.let_go()

    def ingest_stored_artifacts(self, app: Dash) -> None:
        """Forever record the refs of blobs the write worker has finished writing into `app`'s data store."""
        if self._results is None:
            return  # no writer: nothing is ever written
        ship_batches(
            self._results,
            functools.partial(self._record_saved, wait_for_data_store(app)),
            BatchParams(flush_size=10, wait_sec=1, ready=self._ingest_ready, flushed=self._ingest_flushed),
            # `_record_refs` already drops (and cleans up after) anything the store rejects outright,
            # so whatever still escapes it -- a locked database, say -- is worth retrying.
            classify=lambda _exc: Failure.TRANSIENT,
        )

    def _record_saved(self, store: DataStore[...], results: list[_Saved | _Gone]) -> None:
        """
        Record a batch of written artifacts' refs, in one span linked to the write of each (it serves them all).

        Only an upload this process still holds is recorded, once: a writer started again, or this batch tried again,
        may hand back one already finished. Done again, this does nothing more. A ref recorded already (a client's
        retry of a batch that partly landed, or an upload taken over from a process that died after recording it), or
        twice in this batch, is finished without being recorded again.
        """
        held: dict[str, _Saved] = {}
        for result in results:
            match result:
                case _Gone(name=name):
                    if self._journal.finish(name):
                        _log.error(
                            "An upload's staged bytes are gone and its blob was never written", name=name
                        )
                case _Saved(name=name) if self._journal.holds(name):
                    held[name] = result
                case _Saved():
                    pass
        recorded = _recorded_refs(store, [item.artifact for item in held.values()]) if held else set[str]()
        saved: list[_Saved] = []
        for item in held.values():
            if item.artifact.ref in recorded:
                self._journal.finish(item.name)
            else:
                recorded.add(item.artifact.ref)
                saved.append(item)
        if not saved:
            return
        waited = [_waited_since(item.written_at) for item in saved]
        started = time.perf_counter()
        outcome = Outcome.FAILURE
        try:
            with span(
                "dlboard.blob.ingest",
                SpanKind.CONSUMER,
                _IngestTrace(items=len(saved), queue_wait=max(waited, default=0.0)),
                links=[item.trace for item in saved],
            ) as active:
                self._record_refs(store, saved, active)
            outcome = Outcome.SUCCESS
        finally:
            _INGESTS.record(time.perf_counter() - started, OutcomeAttributes(outcome=outcome))
        # Only once recorded: `ship_batches` retries a batch that failed, and each artifact waited once.
        for wait in waited:
            _INGEST_QUEUE_WAIT.record(wait)

    def _record_refs(self, store: DataStore[...], saved: list[_Saved], active: ActiveSpan) -> None:
        """
        Record `saved`'s refs and finish their uploads; one the store rejects outright is dropped alone, its blob deleted.

        `log_artifact_refs` raises `ValueError` for input it will never accept (e.g. a run deleted
        while its upload was in flight) and checks the whole batch before writing any of it -- so on
        one, retry each artifact alone: only the rejected ones are dropped, and their already-written
        blobs deleted rather than orphaned with nothing referencing them.
        """
        try:
            store.log_artifact_refs([item.artifact for item in saved])
        except ValueError:
            if len(saved) > 1:
                active.event("split")
                for item in saved:
                    self._record_refs(store, [item], active)
                return
            _log.exception("The data store rejected artifact %s, deleting its blob", saved[0].artifact.ref)
            self.delete_artifact(AnyUrl(saved[0].artifact.ref))
        for item in saved:
            self._journal.finish(item.name)

    @classmethod
    def get_or_create(cls, backend: BlobBackend, store_q_size: int, staging_dir: Path) -> Self:
        """Create a new store on top of `backend`."""
        return cls(backend, store_q_size, staging_dir)

    def log_artifacts(self, artifacts: Iterable[tuple[NewArtifact, FileStorage]]) -> None:
        """
        Keep each uploaded file in the journal, then queue it for the write worker.

        Once this returns, every upload in the batch is on disk and the server's to finish: the client may discard its copy.

        Safe to send again: an upload whose blob is written already, or that this batch holds twice, is kept as it is,
        and one sent again before its blob is written is recorded once (`_record_saved`). So a client's retry of a batch
        that failed partway, or whose answer was lost, costs nothing twice. A key, step and file name logged again with
        other bytes keeps the first.
        """
        with self._writer_lock:
            writer = self._writer
        if writer is None or not writer.process.is_alive():
            _REJECTIONS.add(1, _RejectionAttributes(reason=_Rejection.WRITER_DOWN))
            msg = "the artifact write worker is not running"
            raise models.ArtifactStoreUnavailableError(msg)
        if not self._journal.wait_for_room(self._limit, _ENQUEUE_TIMEOUT_SEC):
            _REJECTIONS.add(1, _RejectionAttributes(reason=_Rejection.QUEUE_FULL))
            msg = "this worker holds as many unwritten uploads as it may"
            raise models.ArtifactStoreUnavailableError(msg)
        accepted: set[AnyUrl] = set()
        try:
            for a, file in artifacts:
                ref = self._backend.ref_for(blob_key(a.experiment_id, a.run_id, a.key, a.step, a.fname))
                if ref in accepted or self._backend.exists(ref):
                    continue
                accepted.add(ref)
                started = time.perf_counter()
                with span("dlboard.blob.stage", SpanKind.INTERNAL):
                    held = self._journal.accept(a, ref, file)
                _STAGING.record(time.perf_counter() - started)
                with self._writer_lock:
                    if self._writer is not None:  # else closed since: the next process to start takes it over
                        self._writer.send(held, carrier())
        except Exception:
            # Answered with a 5xx (and logged) by the app; what was accepted before it is kept, for the retry to find.
            _REJECTIONS.add(1, _RejectionAttributes(reason=_Rejection.FAILED))
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


def plug_blob_store(app: Dash, backend: BlobBackend, store_q_size: int, staging_dir: Path) -> None:
    """
    Set `app`'s artifact store to a `BlobArtifactStore` over `backend`, and start ingesting its refs.

    In the custodian process, which takes no upload, the store has no writer and nothing to ingest.
    """
    writer = not in_custodian()
    store = BlobArtifactStore(backend, store_q_size, staging_dir, writer=writer)
    set_artifact_store(app, store)
    if writer:
        Thread(
            target=store.ingest_stored_artifacts, args=(app,), daemon=True, name="artifact-ref-ingest"
        ).start()
