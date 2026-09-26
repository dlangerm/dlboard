"""Filesystem artifact store."""

from __future__ import annotations

import functools
import hashlib
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING, ClassVar, Mapping, Self, override
from uuid import uuid4

from flask import Response, send_from_directory
from pydantic import AnyUrl
from pydantic_settings import BaseSettings
from structlog.stdlib import get_logger

from dltrack import models
from dltrack._batching import BatchParams, ship_batches
from dltrack._mp_context import SPAWN_CONTEXT
from dltrack.serve import set_artifact_store, wait_for_data_store

if TYPE_CHECKING:
    from collections.abc import Iterable
    from multiprocessing import Queue

    from dash import Dash
    from werkzeug.datastructures import FileStorage

    from dltrack.models import DataStore
    from dltrack.models._artifact import Artifact, NewArtifact


_log = get_logger(__name__)


def _save_artifact(
    protocol: str,
    root: Path,
    input_q: Queue[tuple[NewArtifact, Path, Path]],
    return_q: Queue[Artifact],
) -> None:
    while True:
        try:
            _a, _target, file = input_q.get()
            if not _target.parent.exists():
                _target.parent.mkdir(parents=True)

            target_artifact_path = _target.with_suffix(f".{_a.fname.split('.')[-1]}")
            artifact = models.Artifact.model_validate(
                _a.model_dump()
                | {
                    "ref": f"{protocol}:///{target_artifact_path.relative_to(root)}",
                }
            )
            shutil.move(file, target_artifact_path)
            if return_q.full():
                _log.warning("Return queue is full, runtime will be impacted")
            return_q.put(artifact)
        except KeyboardInterrupt:
            return
        except Exception:  # noqa: BLE001
            _log.exception("Failed to save artifacts")


class FSArtifactStore(models.ArtifactStore[Path, int]):
    """Store artifacts in the filesystem."""

    protocol: ClassVar[str] = "file"

    def __init__(self, root: Path, store_q_size: int) -> None:
        """Initialize the class with a root directory."""
        self._root_directory = root
        self._store_q: Queue[tuple[NewArtifact, Path, Path]] = SPAWN_CONTEXT.Queue(store_q_size)
        self._saved_artifact_q: Queue[Artifact] = SPAWN_CONTEXT.Queue(store_q_size)
        self._store_artifact_proc = SPAWN_CONTEXT.Process(
            target=_save_artifact,
            args=(self.protocol, root, self._store_q, self._saved_artifact_q),
            daemon=True,
        )
        self._file_staging_dir = Path(tempfile.gettempdir())
        self._store_artifact_proc.start()

    def ingest_stored_artifacts(self, app: Dash) -> None:
        """Forever record the refs of blobs `_save_artifact` has finished writing into `app`'s data store."""
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
        blobs deleted rather than orphaned on disk with nothing referencing them.
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
    def get_or_create(cls, root_directory: Path, store_q_size: int) -> Self:
        """Create a new store."""
        root_directory.mkdir(parents=True, exist_ok=True)
        return cls(root=root_directory, store_q_size=store_q_size)

    def _get_fname(self, experiment_id: int, run_id: int, key: str, step: int | None, fname: str) -> Path:
        fname_hsh = hashlib.shake_256(fname.encode(), usedforsecurity=False)
        key_hsh = hashlib.shake_256(key.encode(), usedforsecurity=False)
        _step = f"{step}" if step is not None else "_anon_"
        return (
            self._root_directory
            / str(experiment_id)
            / str(run_id)
            / key_hsh.hexdigest(16)
            / _step
            / fname_hsh.hexdigest(16)
        )

    def log_artifacts(
        self,
        artifacts: Iterable[NewArtifact],
        files: Mapping[str, FileStorage],
    ) -> None:
        """Log an artifact to the store."""
        try:
            for a in artifacts:
                target = self._get_fname(
                    a.experiment_id,
                    a.run_id,
                    a.key,
                    a.step,
                    a.fname,
                )
                if target.exists():
                    msg = f"duplicate key fname uploaded! {target}"
                    raise ValueError(msg)  # noqa: TRY301

                if self._store_q.full():
                    _log.warning("Store queue is full, runtime will be impacted")
                staging_path = self._file_staging_dir / str(uuid4())
                files[a.key].save(staging_path, buffer_size=int(1e6))
                self._store_q.put((a, target, staging_path))

        except Exception:
            _log.exception("failed to save artifact batch, reverting to allow client retry")
            raise

    @override
    def download_artifact(self, ref: AnyUrl) -> Response:
        assert ref.scheme == self.protocol
        _log.debug("Downloading protocol %s %s in folder %s", ref.scheme, ref.path, self._root_directory)
        return send_from_directory(self._root_directory, str(ref.path).lstrip("/"))

    @override
    def delete_artifact(self, ref: AnyUrl) -> None:
        assert ref.scheme == self.protocol
        path = self._root_directory / str(ref.path).lstrip("/")
        _log.info("Deleting artifact blob at %s", path)
        path.unlink(missing_ok=True)


class AppSettings(BaseSettings):
    """Environment variables."""

    artifact_store_location: Path = Path.home() / ".dltrack_artifacts"
    filesystem_store_queue_size: int = 100


def plug(app: Dash) -> None:
    """Plugin content."""
    env = AppSettings()
    store = FSArtifactStore.get_or_create(env.artifact_store_location, env.filesystem_store_queue_size)
    set_artifact_store(app, store)
    Thread(target=store.ingest_stored_artifacts, args=(app,), daemon=True, name="artifact-ref-ingest").start()
