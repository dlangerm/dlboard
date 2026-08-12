"""Filesystem artifact store."""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Iterable
from multiprocessing import Lock
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Mapping, Self, override

from flask import Response, send_from_directory
from structlog.stdlib import get_logger

from dltrack import models
from dltrack.plugins.utilities._data_store import get_data_store, set_artifact_store

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dash import Dash
    from pydantic import AnyUrl
    from werkzeug.datastructures import FileStorage

    from dltrack.models._artifact import Artifact, NewArtifact


_log = get_logger(__name__)
_FS_LOCK = Lock()


class FSArtifactStore(models.ArtifactStore[Path]):
    """Store artifacts in the filesystem."""

    protocol: ClassVar[str] = "file"

    def __init__(self, root: Path) -> None:
        """Initialize the class with a root directory."""
        self._root_directory = root

    @classmethod
    def get_or_create(cls, root_directory: Path) -> Self:
        """Create a new store."""
        root_directory.mkdir(parents=True, exist_ok=True)
        return cls(root=root_directory)

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
        store = get_data_store()
        uris: list[Artifact] = []
        saved_files: list[Path] = []
        with _FS_LOCK:
            try:
                for a in artifacts:
                    file = files[a.key]
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

                    if not target.parent.exists():
                        target.parent.mkdir(parents=True)

                    target_artifact_path = target.with_suffix(f".{a.fname.split('.')[-1]}")
                    _log.info("Saving artifact at path %s", target_artifact_path)
                    uris.append(
                        models.Artifact.model_validate(
                            a.model_dump()
                            | {
                                "ref": f"{self.protocol}:///{target_artifact_path.relative_to(self._root_directory)}",
                            }
                        )
                    )
                    file.save(target_artifact_path)
                    saved_files.append(target_artifact_path)
                store.log_artifact_refs(uris)
            except Exception:
                _log.exception("failed to save artifact batch, reverting to allow client retry")
                for f in saved_files:
                    with contextlib.suppress(BaseException):
                        f.unlink()
                raise

    @override
    def download_artifact(self, ref: AnyUrl) -> Response:
        assert ref.scheme == self.protocol
        _log.info("Downloading protocol %s %s in folder %s", ref.scheme, ref.path, self._root_directory)
        return send_from_directory(self._root_directory, str(ref.path).lstrip("/"))


def get_plugin(artifact_root: Path) -> models.PluginProtocol:
    """Get the underlying plugin."""

    class Plugin:
        @classmethod
        def plug(cls, app: Dash) -> None:
            """Plugin content."""
            set_artifact_store(app, FSArtifactStore.get_or_create(artifact_root))

    return Plugin
