"""
Retention: each project's (and experiment's) rule for how many runs, or how old, it keeps; kept runs; the worker's lease.

Two new tables, two nullable columns on `Run` (`NULL`: not kept) and four on the single `AppState` row (`NULL`: no
worker has run yet), so every existing row is untouched: expand-only, per the policy in `docs/compatibility.md`. An older
server never reads any of them, and never enforces a policy. A project from before this has no policy row at all: the
worker gives it the server's default, after the grace period, rather than this migration deciding one.

The partial `deleted_at` indexes find what has been in the Trash longest without reading the rows that are not in it,
which are nearly all of them; `idx_Run_kept` finds an experiment's kept runs, few, without reading its runs, and `idx_Run_live` its live runs
(what retention counts) without reading its Trash, which retention fills.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op  # pyrefly: ignore [implicit-reexport]

from dlboard.serve import sql

# revision identifiers, used by Alembic.
revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_INT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_SOFT_DELETABLE = ("Artifact", "Experiment", "Project", "Run")
_APP_STATE_COLUMNS = (
    sa.Column("retention_lease_holder", sa.Text(), nullable=True),
    sa.Column("retention_lease_until", sql.UTCDateTime(timezone=True), nullable=True),
    sa.Column("retention_swept_at", sql.UTCDateTime(timezone=True), nullable=True),
    sa.Column("trash_retention_since", sql.UTCDateTime(timezone=True), nullable=True),
)


def upgrade() -> None:
    """Add the retention tables, kept runs, and the worker's state."""
    op.create_table(
        "RetentionPolicy",
        sa.Column("project_id", _INT, nullable=False),
        sa.Column("experiment_id", _INT, nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("max_runs", _INT, nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("max_age_days", _INT, nullable=True),
        sa.Column("cap_exempt", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("set_by", _INT, nullable=True),
        sa.Column("set_at", sql.UTCDateTime(timezone=True), nullable=False),
        sa.Column("effective_at", sql.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", _INT, sa.Identity(), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["Experiment.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["Project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["set_by"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_RetentionPolicy_scope",
        "RetentionPolicy",
        ["project_id", "experiment_id", "effective_at"],
        unique=False,
    )
    op.create_table(
        "RetentionStatus",
        sa.Column("project_id", _INT, nullable=False),
        sa.Column("checked_at", sql.UTCDateTime(timezone=True), nullable=False),
        sa.Column("overdue_runs", _INT, server_default=sa.text("0"), nullable=False),
        sa.Column("out_of_spec_since", sql.UTCDateTime(timezone=True), nullable=True),
        sa.Column("id", _INT, sa.Identity(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["Project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("Run") as run:
        run.add_column(sa.Column("kept_at", sql.UTCDateTime(timezone=True), nullable=True))
        run.add_column(sa.Column("kept_by", _INT, nullable=True))
        # Named, as a batch (SQLite's rebuilt table) needs it to be; the models never name one, so nothing compares it.
        run.create_foreign_key(
            "fk_Run_kept_by_User", "User", ["kept_by"], ["id"], deferrable=True, initially="DEFERRED"
        )
    op.create_index(
        "idx_Run_live",
        "Run",
        ["experiment_id", "created_at"],
        unique=False,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "idx_Run_kept",
        "Run",
        ["experiment_id"],
        unique=False,
        sqlite_where=sa.text("kept_at IS NOT NULL"),
        postgresql_where=sa.text("kept_at IS NOT NULL"),
    )
    for table in _SOFT_DELETABLE:
        op.create_index(
            f"idx_{table}_deleted",
            table,
            ["deleted_at"],
            unique=False,
            sqlite_where=sa.text("deleted_at IS NOT NULL"),
            postgresql_where=sa.text("deleted_at IS NOT NULL"),
        )
    for column in _APP_STATE_COLUMNS:
        op.add_column("AppState", column)


def downgrade() -> None:
    """Drop them again."""
    for column in reversed(_APP_STATE_COLUMNS):
        op.drop_column("AppState", column.name)
    for table in _SOFT_DELETABLE:
        op.drop_index(f"idx_{table}_deleted", table_name=table)
    op.drop_index("idx_Run_kept", table_name="Run")
    op.drop_index("idx_Run_live", table_name="Run")
    with op.batch_alter_table("Run") as run:
        run.drop_constraint("fk_Run_kept_by_User", type_="foreignkey")
        run.drop_column("kept_by")
        run.drop_column("kept_at")
    op.drop_table("RetentionStatus")
    op.drop_index("idx_RetentionPolicy_scope", table_name="RetentionPolicy")
    op.drop_table("RetentionPolicy")
