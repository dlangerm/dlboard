"""Generic artifact class."""

from __future__ import annotations

import mimetypes
import typing
from typing import Final, TypeAlias

import pendulum
from pydantic import AnyUrl, AwareDatetime, BaseModel, Field

from dlboard._compat import StrEnum

if typing.TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

FILE_KIND_TAG: Final = "file_kind"
"""The tag a file artifact records its `FileKind` under, for anything consuming or displaying it."""


class FileKind(StrEnum):
    """
    What a logged file artifact is -- a bare extension says too little (`.pt`, `.ckpt`, ...).

    Stored as the value of the `FILE_KIND_TAG` tag, so a value is never renamed or removed.
    """

    CHECKPOINT = "checkpoint"
    """A model checkpoint, e.g. one `ModelCheckpoint` saved."""


INLINEABLE_ARTIFACT_CONTENT_TYPES: Final = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
"""
Content types safe for a browser to render inline, rather than only download, when serving an
artifact's blob back (`/artifact/<id>`, any `ArtifactStore.download_artifact` implementation).

An artifact's `fname` (and so its guessed content type) is entirely client-supplied
(`NewArtifact.fname`, `AnyArtifact.to_artifact`), so anyone who can log an artifact can upload
anything, including an HTML or SVG file (`image/svg+xml` can carry an embedded `<script>`) crafted
to run script in this origin the moment someone else opens it. Everything not in this set is served
as `Content-Disposition: attachment` instead.
"""


TagValue: TypeAlias = str | int | float | bool
"""What a caller may put in a tag. Stored as text: see `format_tags`."""

_TAG_FLOAT_FORMAT: Final = "#.4g"
"""Four significant digits, trailing zeros kept, so a column of scores lines up (`0.1410`, `0.1438`)."""


def format_tags(tags: Mapping[str, TagValue]) -> dict[str, str]:
    """
    `tags` as the text they are stored and shown as, numbers and booleans formatted the same way everywhere.

    Callers used to stringify numbers themselves, each their own way (`:.3f`, `:.6g`, ...), so one
    panel's captions disagreed with each other. The wire and the stored value stay text: a numeric
    tag would be rejected by an older server within the supported version skew.
    """
    return {key: _format_tag_value(value) for key, value in tags.items()}


def _format_tag_value(value: TagValue) -> str:
    match value:
        case bool():
            return "true" if value else "false"
        case float():
            return format(value, _TAG_FLOAT_FORMAT)
        case int() | str():
            return str(value)


def is_inlineable_artifact(fname: str) -> bool:
    """Whether a browser shows an artifact named `fname` inline (an image), rather than only offering it as a download."""
    return mimetypes.guess_type(fname)[0] in INLINEABLE_ARTIFACT_CONTENT_TYPES


@typing.runtime_checkable
class AnyArtifact(typing.Protocol):
    """Any logged artifact."""

    @property
    def key(self) -> str:
        """Artifact key, unique keys show up as a separate chart."""
        ...

    @property
    def tags(self) -> Mapping[str, TagValue]:
        """Tags for this artifact; numbers and booleans are stored as text, see `format_tags`."""
        ...

    @property
    def step(self) -> int:
        """Global step where this artifact was logged."""
        ...

    def to_artifact(
        self, local_temp: Path, run_id: int, experiment_id: int
    ) -> tuple[NewArtifact, Path | AnyUrl]:
        """
        Convert this artifact to a `NewArtifact`, plus either a local file to upload or a ref to link.

        A `Path` (written under `local_temp`) is uploaded; an `AnyUrl` is registered as-is, with no
        bytes moved -- for an artifact kind (`client.artifacts.link.Link`) that logs a blob it put
        in the store's own space some other way.
        """
        ...


class NewArtifact(BaseModel, frozen=True, extra="ignore"):
    """An artifact. `extra="ignore"`: this crosses the wire -- see `dlboard._wire`."""

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

    created_by: int | None = None
    """The user who logged this artifact, if known."""

    created_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))
    """When this artifact was logged."""


class NewArtifactLink(NewArtifact, frozen=True, extra="ignore"):
    """A `NewArtifact` for a blob a client has already put in the store's own space, by its ref."""

    ref: AnyUrl
    """Where the blob already lives. The server only accepts one inside its own `ArtifactStore`'s
    space (or an allowlisted read-only location), never an arbitrary ref -- see
    `ArtifactStore.link_artifacts`."""


class Artifact(NewArtifact, frozen=True, extra="ignore"):
    """Underlying table of artifacts. `extra="ignore"`: this crosses the wire -- see `dlboard._wire`."""

    id: int | None = None
    """ID in the database."""

    ref: str
    """The underlying storage of the data if it is already uploaded."""

    deleted_by: int | None = None
    """The user who soft-deleted this artifact, if it's been deleted."""

    deleted_at: AwareDatetime | None = None
    """When this artifact was soft-deleted, if at all. See `Project.deleted_at`."""


class ArtifactStoreUnavailableError(RuntimeError):
    """Raised when an `ArtifactStore` can't accept an upload right now (its write worker died, or its queue stays full); retry later."""


class UnservableArtifactRefError(ValueError):
    """Raised when a linked artifact's `ref` isn't one this server's `ArtifactStore` can serve."""
