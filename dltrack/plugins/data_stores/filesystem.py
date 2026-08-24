"""Filesystem artifact store."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import time
from collections.abc import Iterable
from contextlib import suppress
from multiprocessing import Queue, get_context
from pathlib import Path
from queue import Empty
from threading import Thread
from typing import TYPE_CHECKING, ClassVar, Mapping, Self, override
from uuid import uuid4

from flask import Response, send_from_directory
from pydantic_settings import BaseSettings
from structlog.stdlib import get_logger

from dltrack import models
from dltrack.serve import set_artifact_store, wait_for_data_store

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dash import Dash
    from pydantic import AnyUrl
    from werkzeug.datastructures import FileStorage

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
            _log.info("Saving artifact at path %s", target_artifact_path)
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
            raise
        except Exception:  # noqa: BLE001
            _log.exception("Failed to save artifacts")


class FSArtifactStore(models.ArtifactStore[Path, int]):
    """Store artifacts in the filesystem."""

    protocol: ClassVar[str] = "file"

    def __init__(self, root: Path, store_q_size: int) -> None:
        """Initialize the class with a root directory."""
        self._root_directory = root
        ctx = get_context("spawn")
        self._store_q: Queue[tuple[NewArtifact, Path, Path]] = ctx.Queue(store_q_size)
        self._saved_artifact_q: Queue[Artifact] = ctx.Queue(store_q_size)
        self._store_artifact_proc = ctx.Process(
            target=_save_artifact,
            args=(self.protocol, root, self._store_q, self._saved_artifact_q),
            daemon=True,
        )
        self._save_artifact_thread = Thread(target=self._ingest_stored_artifacts, daemon=True)
        self._file_staging_dir = Path(tempfile.gettempdir())
        self._save_artifact_thread.start()
        self._store_artifact_proc.start()

    def _ingest_stored_artifacts(self) -> None:
        try:
            store = wait_for_data_store()
        except Exception:
            _log.exception("failed to get data store")
            raise
        return_q_batchsize = 10
        maxwait = 1
        tlast = time.perf_counter()
        cur_batch: list[Artifact] = []
        while True:
            try:
                with suppress(Empty):
                    new_artifact = self._saved_artifact_q.get(timeout=maxwait)
                    cur_batch.append(new_artifact)

                if len(cur_batch) >= return_q_batchsize or time.perf_counter() - tlast > maxwait:
                    store.log_artifact_refs(cur_batch)
                    tlast = time.perf_counter()
                    cur_batch.clear()
            except Exception:  # noqa: BLE001
                _log.exception("Failed to store an artifact ref")
                cur_batch.clear()

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

    set_artifact_store(
        app, FSArtifactStore.get_or_create(env.artifact_store_location, env.filesystem_store_queue_size)
    )
