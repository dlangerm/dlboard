"""
When the server recorded each run's end, by its own clock: so what ended since a moment can be found (webhooks, #704).

One nullable column on `Run` (`NULL`: running, or ended before this) and a partial index over the runs that have one, so
every existing row is untouched: expand-only, per the policy in `docs/compatibility.md`. An older server neither writes
nor reads it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op  # pyrefly: ignore [implicit-reexport]

from dlboard.serve import sql

# revision identifiers, used by Alembic.
revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_DONE = sa.text("done_at IS NOT NULL AND deleted_at IS NULL")


def upgrade() -> None:
    """Add the column and the index."""
    with op.batch_alter_table("Run") as run:
        run.add_column(sa.Column("done_at", sql.UTCDateTime(timezone=True), nullable=True))
    op.create_index(
        "idx_Run_done", "Run", ["done_at", "id"], unique=False, sqlite_where=_DONE, postgresql_where=_DONE
    )


def downgrade() -> None:
    """Drop them again."""
    op.drop_index("idx_Run_done", table_name="Run")
    with op.batch_alter_table("Run") as run:
        run.drop_column("done_at")
