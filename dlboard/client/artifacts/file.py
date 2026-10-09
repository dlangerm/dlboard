"""Log an arbitrary file that already exists on disk (a checkpoint, say), uploaded as-is."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from dlboard.models import FILE_KIND_TAG, FileKind, NewArtifact


class File(BaseModel, frozen=True, extra="forbid"):
    """
    A file on disk to upload without re-encoding it.

    `path` must still exist when the background shipper reads it -- it's uploaded in place, not
    copied, so don't delete or overwrite it until the logger has flushed.
    """

    key: str
    """Key for this artifact."""
    path: Path
    """The file to upload; its name becomes the artifact's `fname`."""
    kind: FileKind
    """What the file is, recorded as a tag (`FILE_KIND_TAG`)."""
    tags: dict[str, str] = {}
    """Tags for the file, for use by plugins."""
    step: int
    """The global step of the trainer."""

    def to_artifact(self, local_temp: Path, run_id: int, experiment_id: int) -> tuple[NewArtifact, Path]:  # noqa: ARG002
        """Pair this file's metadata with its path -- nothing is written under `local_temp`."""
        obj = NewArtifact(
            key=self.key,
            fname=self.path.name,
            run_id=run_id,
            experiment_id=experiment_id,
            step=self.step,
            tags={**self.tags, FILE_KIND_TAG: self.kind},
        )
        return obj, self.path
