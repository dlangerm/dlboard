"""Log a blob a client has already stored somewhere itself, by its ref -- no upload."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from pydantic import AnyUrl, BaseModel

from dlboard.models import NewArtifact, TagValue, format_tags

if TYPE_CHECKING:
    from pathlib import Path


class Link(BaseModel, frozen=True, extra="forbid"):
    """
    An artifact whose bytes already live at `ref`.

    E.g. a blob a training job wrote straight to the same S3 bucket dlboard's `ArtifactStore`
    uses, instead of shipping it through dlboard.
    """

    key: str
    """Key for this artifact."""
    ref: AnyUrl
    """Where the blob already lives. Must be inside the server's own `ArtifactStore` space (or one
    of its allowlisted read-only locations) -- see `ArtifactStore.link_artifacts`."""
    tags: Mapping[str, TagValue] = {}
    """Tags for the artifact, for use by plugins."""
    step: int

    def to_artifact(self, local_temp: Path, run_id: int, experiment_id: int) -> tuple[NewArtifact, AnyUrl]:  # noqa: ARG002
        """Pair this link's metadata with its ref -- nothing is written under `local_temp`."""
        obj = NewArtifact(
            key=self.key,
            fname=PurePosixPath(str(self.ref.path or self.ref)).name,
            run_id=run_id,
            experiment_id=experiment_id,
            step=self.step,
            tags=format_tags(self.tags),
        )
        return obj, self.ref
