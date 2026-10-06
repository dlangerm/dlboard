"""Filesystem artifact store: a `BlobBackend` that keeps every blob under one root directory."""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from flask import send_from_directory
from pydantic import AnyUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard.plugins.data_stores._blob_store import RefAccess, plug_blob_store

if TYPE_CHECKING:
    from dash import Dash
    from flask import Response


class FSBlobs:
    """Keep every blob under `root`, on local disk."""

    def __init__(self, root: Path) -> None:
        """Initialize the backend, creating `root` if it doesn't exist yet."""
        root.mkdir(parents=True, exist_ok=True)
        self._root = root.resolve()

    def _resolve(self, ref: AnyUrl) -> Path | None:
        """The local path `ref` names, or `None` if it's not a `file:` ref inside `self._root`."""
        if ref.scheme != "file":
            return None
        candidate = (self._root / str(ref.path).lstrip("/")).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            return None
        return candidate

    def ref_for(self, key: PurePosixPath) -> AnyUrl:
        """The ref a freshly written blob at `key` will get."""
        return AnyUrl(f"file:///{key}")

    def access(self, ref: AnyUrl) -> RefAccess | None:
        """`OWNED` for anything inside `self._root`; `None` (unservable) for anything else."""
        return RefAccess.OWNED if self._resolve(ref) is not None else None

    def exists(self, ref: AnyUrl) -> bool:
        """Whether a blob is already stored at `ref`."""
        path = self._resolve(ref)
        return path is not None and path.exists()

    def write(self, staged: Path, ref: AnyUrl) -> None:
        """Move `staged`'s bytes to `ref`, consuming `staged`."""
        path = self._resolve(ref)
        assert path is not None, f"refusing to write outside {self._root}: {ref}"
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(staged, path)

    def download(self, ref: AnyUrl) -> Response:
        """Serve `ref`'s bytes to a browser."""
        return send_from_directory(self._root, str(ref.path).lstrip("/"))

    def delete(self, ref: AnyUrl) -> None:
        """Permanently delete the blob at `ref`. Idempotent: a blob already gone is not an error."""
        path = self._resolve(ref)
        if path is not None:
            path.unlink(missing_ok=True)


class AppSettings(BaseSettings):
    """`DLBOARD_*` environment variables."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    artifact_store_location: Path = Path.home() / ".dlboard_artifacts"
    filesystem_store_queue_size: int = 100


def plug(app: Dash) -> None:
    """Plugin content."""
    env = AppSettings()
    plug_blob_store(app, FSBlobs(env.artifact_store_location), env.filesystem_store_queue_size)
