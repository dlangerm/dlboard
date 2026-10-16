"""
Histograms: a distribution logged at a step, already binned by the client.

One new table, so every existing row is untouched: expand-only, per the policy in `docs/compatibility.md`.
An older server never reads it, and simply ignores the `histograms` a newer client adds to a metrics batch.

A histogram's `edges` and `counts` are stored as packed binary arrays (see `sql.PackedFloatArray` and
`sql.PackedCountArray`), not JSON text: a bucket edge is 8 bytes instead of about 22 characters.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op  # pyrefly: ignore [implicit-reexport]

from dlboard.serve import sql

# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the histogram table and the index that reads it."""
    op.create_table(
        "UnderlyingHistogramTableEntry",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("experiment_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("run_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("step", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("timestamp_utc", sql.UTCDateTime(timezone=True), nullable=False),
        sa.Column("edges", sql.PackedFloatArray(), nullable=False),
        sa.Column("counts", sql.PackedCountArray(), nullable=False),
        sa.Column("nonfinite", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["Experiment.id"], initially="DEFERRED", deferrable=True),
        sa.ForeignKeyConstraint(["run_id"], ["Run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_histogram_lookup",
        "UnderlyingHistogramTableEntry",
        ["experiment_id", "key", "run_id", "step"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the table and its index."""
    op.drop_index("idx_histogram_lookup", table_name="UnderlyingHistogramTableEntry")
    op.drop_table("UnderlyingHistogramTableEntry")
