"""Generic artifact class."""

from enum import StrEnum, auto

from pydantic import BaseModel


class ArtifactStorageClass(StrEnum):
    """How the artifact is stored."""

    file = auto()
    blob = auto()


class ArtifactType(StrEnum):
    """A type of artifact for logging."""

    image = auto()


class NewArtifact(BaseModel, frozen=True, extra="forbid"):
    """An artifact."""

    artifact_type: ArtifactType
    """Type of artifact."""

    storage_class: ArtifactStorageClass
    """How the artifact should be stored."""

    ref: str | None
    """The url if the artifact is a file."""

    data: bytes | None
    """The data if the artifact is a blob."""


class Artifact(NewArtifact, frozen=True, extra="forbid"):
    """Underlying table of artifacts."""

    id: int
