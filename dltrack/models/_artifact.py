"""Generic artifact class."""

import json

from pydantic import BaseModel, field_validator


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

    @field_validator("tags", mode="before")
    @classmethod
    def _str_to_json(cls, tags: str | dict[str, str]) -> dict[str, str]:
        if isinstance(tags, str):
            return json.loads(tags)
        return tags


class Artifact(NewArtifact, frozen=True, extra="forbid"):
    """Underlying table of artifacts."""

    id: int | None = None
    """ID in the database."""

    ref: str
    """The underlying storage of the data if it is already uploaded."""
