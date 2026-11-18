"""
Run contexts: what produced a run -- its commit, its command line, its packages, the machine it ran on.

One new table, so every existing row is untouched: expand-only, per the policy in `docs/compatibility.md`. An older server
never reads it, and answers a newer client's context with a 404 the client only warns about. Its unique `run_id` (a run
keeps its first context) is also how a page finds the contexts of the runs it shows; purging a run cascades to its own.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op  # pyrefly: ignore [implicit-reexport]
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_INT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    """Add the run context table."""
    op.create_table(
        "RunContext",
        sa.Column("run_id", _INT, nullable=False),
        sa.Column("experiment_id", _INT, nullable=False),
        sa.Column("git_commit", sa.Text(), nullable=True),
        sa.Column("git_branch", sa.Text(), nullable=True),
        sa.Column("git_remote", sa.Text(), nullable=True),
        sa.Column("git_dirty", sa.Boolean(), nullable=True),
        sa.Column("command", _JSON, nullable=False),
        sa.Column("cwd", sa.Text(), nullable=True),
        sa.Column("executable", sa.Text(), nullable=True),
        sa.Column("python_version", sa.Text(), nullable=True),
        sa.Column("platform", sa.Text(), nullable=True),
        sa.Column("hostname", sa.Text(), nullable=True),
        sa.Column("packages", _JSON, nullable=False),
        sa.Column("gpus", _JSON, nullable=False),
        sa.Column("cuda_version", sa.Text(), nullable=True),
        sa.Column("id", _INT, sa.Identity(), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["Experiment.id"], initially="DEFERRED", deferrable=True),
        sa.ForeignKeyConstraint(["run_id"], ["Run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id"),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    """Drop the run context table."""
    op.drop_table("RunContext")
