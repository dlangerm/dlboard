"""Generic artifact class."""

from __future__ import annotations

import typing

from pydantic import BaseModel

if typing.TYPE_CHECKING:
    from pathlib import Path


@typing.runtime_checkable
class LoggedArtifact(typing.Protocol):
    """Any logged artifact."""

    @property
    def key(self) -> str:
        """Artifact key, unique keys show up as a separate chart."""
        ...

    @property
    def tags(self) -> dict[str, str]:
        """Tags for this artifact."""
        ...

    @property
    def step(self) -> int:
        """Global step where this artifact was logged."""
        ...

    def to_artifact(self, local_temp: Path, run_id: int, experiment_id: int) -> tuple[NewArtifact, Path]:
        """Convert this artifact to a new artifact."""
        ...


class NewArtifact(BaseModel, frozen=True, extra="forbid"):
    """An artifact."""

    key: str
    """Key of this artifact for querying."""

    fname: str
    """The key of requests.files"""

    tags: dict[str, str] = {}
    """Tags for this artifact for use by anything consuming or displaying it."""

    run_id: int
    """Run ID for this artifact."""

    experiment_id: int
    """Experiment ID for this artifact."""

    step: int | None
    """If given, log this artifact for a particular step, useful for visualization."""


class Artifact(NewArtifact, frozen=True, extra="forbid"):
    """Underlying table of artifacts."""

    id: int | None = None
    """ID in the database."""

    ref: str
    """The underlying storage of the data if it is already uploaded."""
