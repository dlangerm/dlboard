"""
Audit log: a record of every destructive/admin action (soft-delete, restore, purge).

Routine "who created this" provenance lives directly on the entity tables (`created_by`/
`created_at` -- see `Project`, `Experiment`, `Run`, `Artifact`) rather than here, to keep this
table focused on the actions that actually need a trail and small regardless of how much ordinary
logging traffic a project sees.
"""

from __future__ import annotations

import pendulum
from pydantic import AwareDatetime, BaseModel, Field

from dltrack._compat import StrEnum


class AuditAction(StrEnum):
    """What kind of destructive/admin action an audit log entry records."""

    SOFT_DELETE = "soft_delete"
    RESTORE = "restore"
    PURGE = "purge"


class EntityType(StrEnum):
    """Which kind of entity an audit log entry's `entity_id` refers to."""

    PROJECT = "project"
    EXPERIMENT = "experiment"
    RUN = "run"
    ARTIFACT = "artifact"


class NewAuditLogEntry(BaseModel, frozen=True, extra="forbid"):
    """An audit log entry about to be recorded."""

    timestamp_utc: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))
    """When the action was taken."""

    user_id: int
    """The user who performed the action."""

    action: AuditAction
    """What kind of action this is."""

    entity_type: EntityType
    """The kind of entity `entity_id` refers to."""

    entity_id: int
    """The id of the entity the action was taken on (the top-level target, not a cascaded child)."""

    details: str = "{}"
    """
    A JSON object with extra context, e.g. cascade counts: `{"Experiment": 3, "Run": 12}`.

    A plain JSON text blob (like `HyperParams.raw_hparams`) rather than a typed field, since what's
    worth recording differs by action and entity type.
    """


class AuditLogEntry(NewAuditLogEntry, frozen=True, extra="forbid"):
    """An audit log entry as stored in the database."""

    id: int
    """The ID of this audit log entry."""
