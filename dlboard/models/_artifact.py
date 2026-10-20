"""Generic artifact class."""

from __future__ import annotations

import mimetypes
import typing
from typing import Final, NamedTuple

import pendulum
from pydantic import AnyUrl, AwareDatetime, BaseModel, Field
from structlog.stdlib import get_logger

from dlboard._compat import StrEnum
from dlboard.models._tag import TagValue

if typing.TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_log = get_logger(__name__)

FILE_KIND_TAG: Final = "file_kind"
"""The tag a file artifact records its `FileKind` under, for anything consuming or displaying it."""

OVERLAY_KEY_SUFFIX: Final = "_$overlay"
"""
What an image's default overlays are logged under, after the image's own key (`samples/a`, `samples/a_$overlay`).

An image chart auto-generated for an image draws the artifacts under its key plus this suffix over it. An image can
have several, each told apart by a name after the suffix (`samples/a_$overlay/truth`, `samples/a_$overlay/prediction`);
`overlay_key` and `OverlayKey.parse` build and read these keys. Any other artifact can still be drawn over a picture by
adding it to the chart's Layers table (`layers` setting).

The `$` keeps a key from ending in it by accident; one that does without being logged through `Overlay` or `Image`
as a layer is warned about by the client (`BasicDlboardAPI.log_artifact_batch`).
"""


UNNAMED_OVERLAY: Final = "unnamed overlay"
"""What an overlay logged with no name (`samples/a_$overlay`) is called wherever a name is shown."""


def overlay_key(image_key: str, name: str | None = None) -> str:
    """The key an overlay of the image under `image_key` is logged under; `name` tells several of them apart."""
    return f"{image_key}{OVERLAY_KEY_SUFFIX}" + (f"/{name}" if name else "")


class OverlayKey(NamedTuple):
    """An overlay's key read back (see `overlay_key`): the image it is drawn over, and its name if it has one."""

    image_key: str
    name: str | None

    @classmethod
    def parse(cls, key: str) -> OverlayKey | None:
        """The image and name `key` holds, or `None` if it is not an overlay's key."""
        image_key, suffix, name = key.partition(OVERLAY_KEY_SUFFIX)
        if not (image_key and suffix) or (name and not name.startswith("/")):
            return None
        return cls(image_key, name.removeprefix("/") or None)

    @property
    def label(self) -> str:
        """What to call the overlay where it is shown: its name, or `UNNAMED_OVERLAY`."""
        return self.name or UNNAMED_OVERLAY


def artifact_display_name(key: str) -> str:
    """What to call the artifact under `key` where it is shown: an overlay by its image and name, the rest as they are."""
    overlay = OverlayKey.parse(key)
    return key if overlay is None else f"{overlay.image_key} · {overlay.label}"


def overlay_image_key(key: str) -> str | None:
    """
    The key of the image `key` is an overlay of, or `None` if it is not an overlay's key.

    Deprecated: released in 0.9.0 under this name, and kept for one release. Use `OverlayKey.parse(key)`, which also
    gives the overlay's name.
    """
    overlay = OverlayKey.parse(key)
    return None if overlay is None else overlay.image_key


class FileKind(StrEnum):
    """
    What a logged file artifact is -- a bare extension says too little (`.pt`, `.ckpt`, ...).

    Stored as the value of the `FILE_KIND_TAG` tag, so a value is never renamed or removed.
    """

    CHECKPOINT = "checkpoint"
    """A model checkpoint, e.g. one `ModelCheckpoint` saved."""


LAYER_KIND_TAG: Final = "layer_kind"
"""The tag an artifact records its `LayerKind` under, when a browser has to be told how to draw it as a layer."""


class LayerKind(StrEnum):
    """
    How a browser draws an artifact as a layer over a picture, for what a file's extension cannot say.

    Stored as the value of the `LAYER_KIND_TAG` tag, so a value is never renamed or removed. An artifact with no such
    tag is a layer only if it is an image, drawn as the picture it is (a segmentation mask, say).
    """

    SHAPES = "shapes"
    """An `Overlay`: shapes as JSON, drawn as an SVG."""
    PICTURE = "picture"
    """An image, drawn as it is."""


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


def is_inlineable_artifact(fname: str) -> bool:
    """Whether a browser shows an artifact named `fname` inline (an image), rather than only offering it as a download."""
    return mimetypes.guess_type(fname)[0] in INLINEABLE_ARTIFACT_CONTENT_TYPES


def layer_kind(tags: Mapping[str, TagValue], fname: str) -> LayerKind | None:
    """How an artifact is drawn as a layer over a picture, or `None` if it cannot be: the kind it says it is (`LAYER_KIND_TAG`), else a picture if it is an image."""
    stored = tags.get(LAYER_KIND_TAG)
    if stored is None:
        return LayerKind.PICTURE if is_inlineable_artifact(fname) else None
    try:
        return LayerKind(stored)
    except ValueError:
        # A kind a newer client logged: it is no layer here, and say why.
        _log.warning("a layer of an unknown kind is not drawn", kind=stored)
        return None


@typing.runtime_checkable
class AnyArtifact(typing.Protocol):
    """Any logged artifact."""

    @property
    def key(self) -> str:
        """Artifact key, unique keys show up as a separate chart."""
        ...

    @property
    def tags(self) -> Mapping[str, TagValue]:
        """Tags for this artifact; numbers and booleans are sent and stored as they are."""
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

    tags: dict[str, TagValue] = {}
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
