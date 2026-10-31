"""
Comparisons: a dashboard of hand-picked runs from any experiments of one project.

A comparison is an owned `Page` scoped to a project (`Page.project_id`, which has always been there), so what is new is
the table of its member runs, and the index that lists a project's comparisons. One new table and an index: expand-only, per
the policy in `docs/compatibility.md`, so an older server simply never reads them.

The member table's unique `(page_id, run_id)` indexes a comparison's runs; purging a run finds the comparisons it is in
through the index on `run_id`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op  # pyrefly: ignore [implicit-reexport]

from dlboard.serve import sql

# revision identifiers, used by Alembic.
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the member table, and the index that lists a project's comparisons."""
    op.create_table(
        "ComparisonRun",
        sa.Column("page_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("run_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("added_by", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("added_at", sql.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.ForeignKeyConstraint(["added_by"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.ForeignKeyConstraint(["page_id"], ["Page.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["Run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("page_id", "run_id"),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_ComparisonRun_run", "ComparisonRun", ["run_id"], unique=False)
    op.create_index("idx_Page_project", "Page", ["project_id", "owner_id"], unique=False)


def downgrade() -> None:
    """Drop the member table and the index."""
    op.drop_index("idx_Page_project", table_name="Page")
    op.drop_index("idx_ComparisonRun_run", table_name="ComparisonRun")
    op.drop_table("ComparisonRun")
