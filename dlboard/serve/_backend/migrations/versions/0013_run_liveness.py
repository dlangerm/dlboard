"""
When each run was last heard from, and when it was given up as lost: so a run whose process died stops reading running.

Two nullable columns on `Run` (`NULL`: never heard from since, and not lost) and a partial index over the runs still
running, so every existing row is untouched: expand-only, per the policy in `docs/compatibility.md`. An older server
never reads them: its runs whose process died stay running, as they always did.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op  # pyrefly: ignore [implicit-reexport]

from dlboard.serve import sql

# revision identifiers, used by Alembic.
revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_WHEN = sql.UTCDateTime(timezone=True)
_RUNNING = sa.text("status = 'running' AND deleted_at IS NULL")


def upgrade() -> None:
    """Add the columns and the index."""
    with op.batch_alter_table("Run") as run:
        run.add_column(sa.Column("last_seen_at", _WHEN, nullable=True))
        run.add_column(sa.Column("lost_at", _WHEN, nullable=True))
    op.create_index(
        "idx_Run_running",
        "Run",
        ["last_seen_at"],
        unique=False,
        sqlite_where=_RUNNING,
        postgresql_where=_RUNNING,
    )


def downgrade() -> None:
    """Drop them again."""
    op.drop_index("idx_Run_running", table_name="Run")
    with op.batch_alter_table("Run") as run:
        run.drop_column("lost_at")
        run.drop_column("last_seen_at")
