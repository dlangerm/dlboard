"""Filesystem artifact store."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from multiprocessing import Lock
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Iterator, Mapping, Self, override

from flask import Response, send_from_directory
from structlog.stdlib import get_logger

from dltrack import models
from dltrack.plugins.utilities._data_store import set_artifact_store

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dash import Dash
    from pydantic import AnyUrl
    from werkzeug.datastructures import FileStorage

    from dltrack.models._artifact import NewArtifact


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

    def _get_metadata_fnames(
        self,
        experiment_id: int | None,
        run_id: int | None,
        key: str | None,
        step: int | None,
        fname: str | None,
    ) -> Iterator[Path]:
        exp_id_glob = str(experiment_id) if experiment_id is not None else "**"
        run_id_glob = str(run_id) if run_id is not None else "**"
        fname_hsh = hashlib.shake_256(fname.encode(), usedforsecurity=False).hexdigest(16) if fname else "**"
        key_hsh = hashlib.shake_256(key.encode(), usedforsecurity=False).hexdigest(16) if key else "**"
        step_glob = f"{step}" if step is not None else "**"

        yield from self._root_directory.glob(
            f"{exp_id_glob}/{run_id_glob}/{key_hsh}/{step_glob}/{fname_hsh}/*.json",
            case_sensitive=True,
        )

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
        for a in artifacts:
            _log.info("logging artifact %s", a.fname)
            file = files[a.fname]
            with _FS_LOCK:
                target = self._get_fname(
                    a.experiment_id,
                    a.run_id,
                    a.key,
                    a.step,
                    a.fname,
                )
                if target.exists():
                    msg = f"duplicate key fname uploaded! {target}"
                    raise ValueError(msg)

                if not target.parent.exists():
                    target.parent.mkdir(parents=True)

                file.save(target.with_suffix("artifact"))
                target.with_suffix("json").write_text(
                    models.Artifact.model_validate(
                        a.model_copy(update={"ref": str(target)})
                    ).model_dump_json()
                )

    @override
    def get_artifacts(
        self,
        keys: set[str] | None = None,
        run_id: int | None = None,
        experiment_id: int | None = None,
        step: int | None = None,
        fname: str | None = None,
    ) -> Iterator[models.Artifact]:
        for k in keys or [None]:
            yield from (
                models.Artifact.model_validate(m.read_text)
                for m in self._get_metadata_fnames(
                    experiment_id=experiment_id,
                    key=k,
                    run_id=run_id,
                    step=step,
                    fname=fname,
                )
            )

    @override
    def download_artifact(self, ref: AnyUrl) -> Response:
        assert ref.scheme == self.protocol
        return send_from_directory(self._root_directory, str(ref.path))


def get_plugin(artifact_root: Path) -> models.PluginProtocol:
    """Get the underlying plugin."""

    class Plugin:
        @classmethod
        def plug(cls, app: Dash) -> None:
            """Plugin content."""
            set_artifact_store(app, FSArtifactStore.get_or_create(artifact_root))

    return Plugin
