"""
An index on a project's live experiments, newest first.

The project page lists a project's experiments a page at a time, the newest first (`search_experiments`), and nothing led
with `project_id`: every read of a project's experiments read the whole table. One new partial index, so every existing
row is untouched: expand-only, per the policy in `docs/compatibility.md`. An older server only reads it as it reads the
table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op  # pyrefly: ignore [implicit-reexport]

# revision identifiers, used by Alembic.
revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the index."""
    op.create_index(
        "idx_Experiment_project",
        "Experiment",
        ["project_id", "created_at"],
        unique=False,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """Drop it again."""
    op.drop_index("idx_Experiment_project", table_name="Experiment")
