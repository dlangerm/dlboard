"""
When a running purge last made progress.

One nullable column on the single `AppState` row, `NULL` while no purge is running, so the admin page can tell a purge
in progress (on any server process) from one a process died in the middle of (#429). Expand-only, per the policy in
`docs/compatibility.md`: an older server that doesn't know the column simply never reads it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op  # pyrefly: ignore [implicit-reexport]

from dlboard.serve import sql

# revision identifiers, used by Alembic.
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add when a running purge last made progress."""
    op.add_column(
        "AppState", sa.Column("artifact_purge_progress_at", sql.UTCDateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Drop it again."""
    op.drop_column("AppState", "artifact_purge_progress_at")
