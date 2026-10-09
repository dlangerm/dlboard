"""
A run's status and when it ended.

Two nullable columns, so every run stored before this keeps working untouched: `status` is `NULL`
for a run that predates status tracking (shown as having none), and `ended_at` is `NULL` until a
client reports the run done. Expand-only, per the policy in `docs/compatibility.md` -- nothing is
dropped or rewritten, and an older server that doesn't know the columns simply never reads them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from dlboard.serve import sql

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the run's status and end time."""
    op.add_column("Run", sa.Column("status", sa.Text(), nullable=True))
    op.add_column("Run", sa.Column("ended_at", sql.UTCDateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Drop them again."""
    op.drop_column("Run", "ended_at")
    op.drop_column("Run", "status")
