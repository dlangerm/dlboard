"""
Queued work to delete a purged artifact's blob from the `ArtifactStore`.

A purge only ever removes rows from the `DataStore` -- the underlying blob (a file on disk, today)
has to be cleaned up separately, since `ArtifactStore` is a plugin boundary and that deletion may
be long-running. One row is written per artifact blob, in the same atomic transaction as the purge
that made it necessary, so the work can never be lost even if the process dies right after.

A row only ever represents outstanding work: a completed task is deleted outright (see
`DataStore.complete_artifact_purge`), not marked done, so this table never accumulates an
ever-growing history -- it only ever holds what's left to clean up. A failed attempt is the one
exception: the row stays, with `last_error` set, so it's visible and retried on the next drain.
"""

from __future__ import annotations

import pendulum
from pydantic import AwareDatetime, BaseModel, Field


class NewArtifactPurgeTask(BaseModel, frozen=True, extra="forbid"):
    """A blob deletion about to be queued."""

    artifact_id: int
    """The id the deleted `Artifact` row had. Informational only -- the row is already gone."""

    ref: str
    """The blob's location, captured before the artifact row (and its `ref`) disappeared."""

    requested_by: int
    """The user who triggered the purge that queued this."""

    requested_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))


class ArtifactPurgeTask(NewArtifactPurgeTask, frozen=True, extra="forbid"):
    """A blob deletion as stored in the database."""

    id: int | None = None

    last_error: str | None = None
    """Set when the most recent attempt to delete this blob failed. A successful attempt deletes
    the row entirely rather than clearing this."""
