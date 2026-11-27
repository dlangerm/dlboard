"""
When the server recorded each run's end, by its own clock: what tells one end of a run from another (webhooks, #704).

One nullable column on `Run` (`NULL`: running, or ended before this), so every existing row is untouched: expand-only,
per the policy in `docs/compatibility.md`. An older server neither writes nor reads it.
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


def upgrade() -> None:
    """Add the column."""
    with op.batch_alter_table("Run") as run:
        run.add_column(sa.Column("done_at", sql.UTCDateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Drop it again."""
    with op.batch_alter_table("Run") as run:
        run.drop_column("done_at")
