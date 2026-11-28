"""
When each user last signed in through their auth provider, and whether their admin came from a group (#822).

`signed_in_at` is nullable (`NULL`: not signed in since this), and `admin_from_group` defaults to false (an admin made
before this is kept, as if made by hand), so every existing row is untouched: expand-only, per the policy in
`docs/compatibility.md`. An older server neither writes nor reads them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op  # pyrefly: ignore [implicit-reexport]

from dlboard.serve import sql

# revision identifiers, used by Alembic.
revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the columns."""
    with op.batch_alter_table("User") as user:
        user.add_column(sa.Column("signed_in_at", sql.UTCDateTime(timezone=True), nullable=True))
        user.add_column(
            sa.Column("admin_from_group", sa.Boolean(), server_default=sa.false(), nullable=False)
        )


def downgrade() -> None:
    """Drop them again."""
    with op.batch_alter_table("User") as user:
        user.drop_column("admin_from_group")
        user.drop_column("signed_in_at")
