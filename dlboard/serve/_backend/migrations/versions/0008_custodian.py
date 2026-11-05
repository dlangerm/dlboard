"""
Custodial work's state: each background task's lease and last run, each custodian's heartbeat, and the work items.

Three new tables, and the blob deletions an older server left queued copied into the new one: expand-only, per the policy
in `docs/compatibility.md`. Every existing row is untouched, and an older server never reads the new tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op  # pyrefly: ignore [implicit-reexport]
from sqlalchemy.dialects import postgresql

from dlboard.serve import sql

# revision identifiers, used by Alembic.
revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_INT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
_WHEN = sql.UTCDateTime(timezone=True)


def upgrade() -> None:
    """Add the task, custodian and work item tables."""
    op.create_table(
        "TaskState",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("holder", sa.Text(), nullable=True),
        sa.Column("lease_until", _WHEN, nullable=True),
        sa.Column("last_started_at", _WHEN, nullable=True),
        sa.Column("last_finished_at", _WHEN, nullable=True),
        sa.Column("last_succeeded_at", _WHEN, nullable=True),
        sa.Column("last_outcome", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("requested_at", _WHEN, nullable=True),
        sa.Column("progress", _JSON, nullable=False),
        sa.Column("id", _INT, sa.Identity(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "Custodian",
        sa.Column("holder", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("pid", _INT, nullable=False),
        sa.Column("started_at", _WHEN, nullable=False),
        sa.Column("heartbeat_at", _WHEN, nullable=False),
        sa.Column("id", _INT, sa.Identity(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("holder"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "WorkItem",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("due_at", _WHEN, nullable=False),
        sa.Column("payload", _JSON, nullable=False),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claimed_until", _WHEN, nullable=True),
        sa.Column("attempts", _INT, server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("id", _INT, sa.Identity(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "key"),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_WorkItem_due", "WorkItem", ["kind", "due_at"], unique=False)
    _queue_pending_blob_deletions()


def _queue_pending_blob_deletions() -> None:
    """
    Copy the blob deletions an older server left queued (`ArtifactPurgeTask`) into the work queue, which now does them.

    Copied, not moved: the old rows stay until a later release drops the table, and an older server rolled back to
    deletes what is left of them again, which is harmless (a blob that is gone is what a deletion was asked for). The
    payload is written here as `BlobDeletion` holds it, rather than imported, as a released migration never changes.
    """
    conn = op.get_bind()
    old = sa.table(
        "ArtifactPurgeTask",
        sa.column("artifact_id", _INT),
        sa.column("ref", sa.Text()),
        sa.column("requested_by", _INT),
        sa.column("requested_at", _WHEN),
    )
    work = sa.table(
        "WorkItem",
        sa.column("kind", sa.Text()),
        sa.column("key", sa.Text()),
        sa.column("due_at", _WHEN),
        sa.column("payload", _JSON),
        sa.column("attempts", _INT),
    )
    rows = [
        {
            "kind": "blob_delete",
            "key": str(artifact_id),
            "due_at": requested_at,
            "payload": {"ref": ref, "requested_by": requested_by},
            "attempts": 0,
        }
        for artifact_id, ref, requested_by, requested_at in conn.execute(
            sa.select(old.c.artifact_id, old.c.ref, old.c.requested_by, old.c.requested_at)
        )
    ]
    if rows:
        conn.execute(sa.insert(work), rows)


def downgrade() -> None:
    """Drop them again."""
    op.drop_index("idx_WorkItem_due", table_name="WorkItem")
    op.drop_table("WorkItem")
    op.drop_table("Custodian")
    op.drop_table("TaskState")
