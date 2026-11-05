"""
Kept runs, the run indexes a retention rule reads, and plugins' scoped settings.

Two nullable columns on `Run` (`NULL`: not kept), two partial indexes on it, and one new table, so every existing row is
untouched: expand-only, per the policy in `docs/compatibility.md`. An older server never reads them: it neither keeps a
run nor protects a kept one.

`idx_Run_live` reads an experiment's live runs from either end without reading its Trash, which retention fills from the
oldest end; `idx_Run_kept` finds an experiment's kept runs, few, without reading its runs. `idx_Run_live` is
`idx_run_experiment` over the live rows only, so it replaces it (a run insert or delete maintains one index, not two):
what read the old one by `experiment_id` alone is the live runs, or a cascade's runs, which `idx_Run_deleted` finds by
their shared `deleted_at`. Dropping an index loses no data, and an older server only reads a little more slowly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op  # pyrefly: ignore [implicit-reexport]
from sqlalchemy.dialects import postgresql

from dlboard.serve import sql

# revision identifiers, used by Alembic.
revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_INT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
_WHEN = sql.UTCDateTime(timezone=True)


def upgrade() -> None:
    """Add kept runs, the run indexes, and the scoped settings table."""
    with op.batch_alter_table("Run") as run:
        run.add_column(sa.Column("kept_at", _WHEN, nullable=True))
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
    op.drop_index("idx_run_experiment", table_name="Run")  # `idx_Run_live` is it, over the live rows only
    op.create_index(
        "idx_Run_kept",
        "Run",
        ["experiment_id"],
        unique=False,
        sqlite_where=sa.text("kept_at IS NOT NULL"),
        postgresql_where=sa.text("kept_at IS NOT NULL"),
    )
    op.create_table(
        "ScopedSetting",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("project_id", _INT, nullable=False),
        sa.Column("experiment_id", _INT, nullable=True),
        sa.Column("value", _JSON, nullable=False),
        sa.Column("updated_by", _INT, nullable=True),
        sa.Column("updated_at", _WHEN, nullable=False),
        sa.Column("id", _INT, sa.Identity(), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["Experiment.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["Project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_ScopedSetting_project",
        "ScopedSetting",
        ["key", "project_id"],
        unique=True,
        sqlite_where=sa.text("experiment_id IS NULL"),
        postgresql_where=sa.text("experiment_id IS NULL"),
    )
    op.create_index(
        "idx_ScopedSetting_experiment",
        "ScopedSetting",
        ["key", "project_id", "experiment_id"],
        unique=True,
        sqlite_where=sa.text("experiment_id IS NOT NULL"),
        postgresql_where=sa.text("experiment_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop them again."""
    op.drop_index("idx_ScopedSetting_experiment", table_name="ScopedSetting")
    op.drop_index("idx_ScopedSetting_project", table_name="ScopedSetting")
    op.drop_table("ScopedSetting")
    op.drop_index("idx_Run_kept", table_name="Run")
    op.drop_index("idx_Run_live", table_name="Run")
    op.create_index("idx_run_experiment", "Run", ["experiment_id", "created_at"], unique=False)
    with op.batch_alter_table("Run") as run:
        run.drop_constraint("fk_Run_kept_by_User", type_="foreignkey")
        run.drop_column("kept_by")
        run.drop_column("kept_at")
