"""
Typed foreign-key relationships between tables.

This is the single source of truth both `_sql.create_table_sql` (SQL schema generation) and
`_sql_store_base._cascade_targets` (soft-delete/restore/purge cascade resolution) read from --
declaring a relationship once here is enough for it to show up correctly in the generated schema
*and* to participate in cascades, with no other code to update.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import BaseModel


class ForeignKeyKind(StrEnum):
    """
    Whether a foreign key column represents ownership or plain attribution.

    OWNERSHIP means the referenced row is this row's parent in the delete/restore/purge hierarchy:
    the column gets a real `ON DELETE CASCADE` in the schema, and it's part of the graph
    `_cascade_targets` walks to resolve what belongs under a given root for soft-delete/restore and
    for purge's audit-log counts. ATTRIBUTION means it's just a reference that never cascades --
    `created_by`/`deleted_by`/`AuditLogEntry.user_id`, or `Artifact.experiment_id`/
    `UnderlyingMetricTableEntry.experiment_id` alongside the "true" ownership edge on `run_id`
    (deleting a user must never cascade-delete everything they created or deleted).
    """

    OWNERSHIP = "ownership"
    ATTRIBUTION = "attribution"


@dataclass(frozen=True)
class ForeignKey:
    """One column's foreign key: which table it references, and what kind of relationship it is."""

    references: type[BaseModel]
    kind: ForeignKeyKind = ForeignKeyKind.ATTRIBUTION
