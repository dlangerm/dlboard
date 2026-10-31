"""
Pins: a flag on a run that says it matters, who flagged it, and in which colour.

One new table, so every existing row is untouched: expand-only, per the policy in `docs/compatibility.md`.
An older server never reads it. Its unique constraint, one pin a user and colour per run, indexes the pins of a run: how a
page finds the pins on the runs it draws, and how purging a run finds the pins to remove. A pin also names its run's experiment,
which a run never leaves, so that a project's pins are found by the project's experiments, few, and an index on
`(experiment_id, pinned_at)`, and never by reading the project's runs, which nothing bounds.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op  # pyrefly: ignore [implicit-reexport]

from dlboard.serve import sql

# revision identifiers, used by Alembic.
revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the pin table."""
    op.create_table(
        "RunPin",
        sa.Column("run_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("experiment_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("pinned_by", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("color", sa.Text(), server_default=sa.text("'plain'"), nullable=False),
        sa.Column("pinned_at", sql.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["Experiment.id"], initially="DEFERRED", deferrable=True),
        sa.ForeignKeyConstraint(["pinned_by"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.ForeignKeyConstraint(["run_id"], ["Run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "pinned_by", "color"),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_RunPin_experiment", "RunPin", ["experiment_id", "pinned_at"], unique=False)


def downgrade() -> None:
    """Drop the pin table."""
    op.drop_index("idx_RunPin_experiment", table_name="RunPin")
    op.drop_table("RunPin")
