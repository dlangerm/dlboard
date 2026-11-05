"""
What purging queues for the custodian: an entity in the Trash to purge, and each artifact blob a purge leaves behind.

The store queues a blob's deletion in the same transaction as the purge that orphans it, so none is ever lost; the
`artifact_purge_worker` plugin's tasks claim and do both kinds of work.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

from dlboard import models
from dlboard._compat import StrEnum
from dlboard.serve._custodian._models import NewWorkItem

REQUESTED_BY: Final = "requested_by"
"""The payload field that says whose purge it is: the work rule binds it to the user who queued the item."""


class PurgeWork(StrEnum):
    """The kinds of work item purging queues. Written to a database column, so a value is never renamed or removed."""

    BLOB = "blob_delete"
    """An artifact's blob, to delete from the artifact store: its row is purged already."""
    ENTITY = "purge"
    """A project, experiment, run or artifact in the Trash, to purge for good: an admin asked for it."""


class BlobDeletion(BaseModel, frozen=True, extra="forbid"):
    """A blob a purge orphaned, and who asked for the purge."""

    ref: str
    requested_by: int


class EntityPurge(BaseModel, frozen=True, extra="forbid"):
    """What in the Trash to purge, and who asked."""

    entity_type: models.EntityType
    entity_id: int
    requested_by: int


def blob_deletion(artifact_id: int, ref: str, requested_by: int) -> NewWorkItem:
    """The work item that deletes an artifact's blob: one an artifact, however often its purge is asked for."""
    return NewWorkItem(
        kind=PurgeWork.BLOB,
        key=str(artifact_id),
        payload=BlobDeletion(ref=ref, requested_by=requested_by).model_dump(mode="json"),
    )


def entity_purge(entity_type: models.EntityType, entity_id: int, requested_by: int) -> NewWorkItem:
    """The work item that purges an entity: one an entity, however often it is asked for."""
    return NewWorkItem(
        kind=PurgeWork.ENTITY,
        key=f"{entity_type}:{entity_id}",
        payload=EntityPurge(
            entity_type=entity_type, entity_id=entity_id, requested_by=requested_by
        ).model_dump(mode="json"),
    )
