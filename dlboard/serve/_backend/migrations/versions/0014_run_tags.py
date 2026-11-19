"""
Run tags: labels a project's runs share (`baseline`, `ablation/lr`), and which runs have which.

Two new tables, so every existing row is untouched: expand-only, per the policy in `docs/compatibility.md`. An older server
never reads them, and ignores the tags a newer client asks for as it launches a run. A tag's unique `(project_id, name)`
is how a project's tags are listed, a page at a time; a run tag's unique `(run_id, tag_id)` how a run's tags are found,
and `idx_RunTag_tag` how a tag's runs of an experiment are, for a list filtered by it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op  # pyrefly: ignore [implicit-reexport]

from dlboard.serve import sql

# revision identifiers, used by Alembic.
revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_INT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_WHEN = sql.UTCDateTime(timezone=True)


def upgrade() -> None:
    """Add the tag tables."""
    op.create_table(
        "Tag",
        sa.Column("project_id", _INT, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", _WHEN, nullable=False),
        sa.Column("id", _INT, sa.Identity(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["Project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "RunTag",
        sa.Column("run_id", _INT, nullable=False),
        sa.Column("tag_id", _INT, nullable=False),
        sa.Column("experiment_id", _INT, nullable=False),
        sa.Column("tagged_by", _INT, nullable=True),
        sa.Column("tagged_at", _WHEN, nullable=False),
        sa.Column("id", _INT, sa.Identity(), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["Experiment.id"], initially="DEFERRED", deferrable=True),
        sa.ForeignKeyConstraint(["run_id"], ["Run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["Tag.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tagged_by"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "tag_id"),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_RunTag_tag", "RunTag", ["tag_id", "experiment_id"], unique=False)


def downgrade() -> None:
    """Drop the tag tables."""
    op.drop_index("idx_RunTag_tag", table_name="RunTag")
    op.drop_table("RunTag")
    op.drop_table("Tag")
