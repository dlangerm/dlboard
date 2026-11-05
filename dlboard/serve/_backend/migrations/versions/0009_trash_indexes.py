"""
Partial indexes on `deleted_at`: what has been in the Trash longest, found without reading what is not in it.

Four new indexes, each over only the soft-deleted rows of its table (nearly none of them), so every existing row is
untouched: expand-only, per the policy in `docs/compatibility.md`. An older server never uses them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op  # pyrefly: ignore [implicit-reexport]

# revision identifiers, used by Alembic.
revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_SOFT_DELETABLE = ("Artifact", "Experiment", "Project", "Run")


def upgrade() -> None:
    """Add an index on each soft-deletable table's deleted rows."""
    for table in _SOFT_DELETABLE:
        op.create_index(
            f"idx_{table}_deleted",
            table,
            ["deleted_at"],
            unique=False,
            sqlite_where=sa.text("deleted_at IS NOT NULL"),
            postgresql_where=sa.text("deleted_at IS NOT NULL"),
        )


def downgrade() -> None:
    """Drop them again."""
    for table in _SOFT_DELETABLE:
        op.drop_index(f"idx_{table}_deleted", table_name=table)
