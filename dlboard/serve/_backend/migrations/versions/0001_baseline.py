"""
Baseline schema: every table and index dlboard had at the moment this became the frozen revision history.

Generated once via `alembic revision --autogenerate` against an empty database (so every column,
type, constraint, and index matches `build_metadata()` exactly as it existed then) and hand-reviewed
-- not the live `build_metadata()` call this revision used to make, and must never go back to. A
revision that reruns the current models can never be replayed correctly once a later revision also
exists: a fresh database would get head's schema from this one, and the next revision would then
try to apply a change that's already there. Literal `op.create_table`/`op.create_index` calls don't
have that problem, since they describe the schema as it was at this point in history, not as the
code happens to describe it today.

This revision is never edited again after release -- see the expand/contract migration policy in
CLAUDE.md. A later schema change is always a new revision.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op  # pyrefly: ignore [implicit-reexport]
from sqlalchemy.dialects import postgresql

from dlboard.serve import sql

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create every table and index."""
    op.create_table(
        "AppState",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.Column("bootstrap_admin_assigned", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "User",
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column(
            "groups",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "scopes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sql.UTCDateTime(timezone=True), nullable=False),
        sa.Column("session_epoch", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.Column("disabled_at", sql.UTCDateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", "subject"),
        sa.UniqueConstraint("username"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "ApiToken",
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_id", sa.Text(), nullable=False),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sql.UTCDateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sql.UTCDateTime(timezone=True), nullable=True),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.Column("last_used_at", sql.UTCDateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sql.UTCDateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "ArtifactPurgeTask",
        sa.Column("artifact_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("requested_at", sql.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["requested_by"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "AuditLogEntry",
        sa.Column("timestamp_utc", sql.UTCDateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("details", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "PasswordCredential",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("updated_at", sql.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "Project",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("everyone_role", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("created_at", sql.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.Column("deleted_by", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("deleted_at", sql.UTCDateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.ForeignKeyConstraint(["deleted_by"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "Experiment",
        sa.Column("project_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("name", sa.Text(), server_default=sa.text("('')"), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("('')"), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("created_at", sql.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.Column(
            "revision",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "notes_revision",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_activity_at", sql.UTCDateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("deleted_at", sql.UTCDateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.ForeignKeyConstraint(["deleted_by"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.ForeignKeyConstraint(["project_id"], ["Project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "ProjectGrant",
        sa.Column("project_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("group", sa.Text(), nullable=True),
        sa.Column("created_at", sql.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["Project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "group"),
        sa.UniqueConstraint("project_id", "user_id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "Comment",
        sa.Column("experiment_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("author_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "run_ids",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "mentioned_user_ids",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sql.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.ForeignKeyConstraint(["author_id"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.ForeignKeyConstraint(["experiment_id"], ["Experiment.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "Run",
        sa.Column("experiment_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("created_at", sql.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.Column("deleted_by", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("deleted_at", sql.UTCDateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.ForeignKeyConstraint(["deleted_by"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.ForeignKeyConstraint(["experiment_id"], ["Experiment.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "Artifact",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("fname", sa.Text(), nullable=False),
        sa.Column(
            "tags",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("run_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("experiment_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("step", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("created_by", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("created_at", sql.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("deleted_by", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("deleted_at", sql.UTCDateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.ForeignKeyConstraint(["deleted_by"], ["User.id"], initially="DEFERRED", deferrable=True),
        sa.ForeignKeyConstraint(["experiment_id"], ["Experiment.id"], initially="DEFERRED", deferrable=True),
        sa.ForeignKeyConstraint(["run_id"], ["Run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_artifact_lookup",
        "Artifact",
        [
            "key",
            "fname",
            "run_id",
            "experiment_id",
            "step",
            "created_by",
            "created_at",
            "ref",
            "deleted_by",
            "deleted_at",
        ],
        unique=False,
    )
    op.create_table(
        "HyperParams",
        sa.Column("run_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("experiment_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("raw_hparams", sa.Text(), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["Experiment.id"], initially="DEFERRED", deferrable=True),
        sa.ForeignKeyConstraint(["run_id"], ["Run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "Page",
        sa.Column("run_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("experiment_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("project_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("owner_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("name", sa.Text(), server_default=sa.text("('')"), nullable=False),
        sa.Column("shared", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "panels",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "page_settings",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["Experiment.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["Project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["Run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_Page_shared_experiment_id",
        "Page",
        ["experiment_id"],
        unique=True,
        sqlite_where=sa.text("owner_id IS NULL"),
        postgresql_where=sa.text("owner_id IS NULL"),
    )
    op.create_index(
        "idx_Page_shared_project_id",
        "Page",
        ["project_id"],
        unique=True,
        sqlite_where=sa.text("owner_id IS NULL"),
        postgresql_where=sa.text("owner_id IS NULL"),
    )
    op.create_index(
        "idx_Page_shared_run_id",
        "Page",
        ["run_id"],
        unique=True,
        sqlite_where=sa.text("owner_id IS NULL"),
        postgresql_where=sa.text("owner_id IS NULL"),
    )
    op.create_table(
        "UnderlyingMetricTableEntry",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.Identity(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Double[float](), nullable=True),
        sa.Column("experiment_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("run_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("step", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("timestamp_utc", sql.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["Experiment.id"], initially="DEFERRED", deferrable=True),
        sa.ForeignKeyConstraint(["run_id"], ["Run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_metric_lookup",
        "UnderlyingMetricTableEntry",
        ["key", "experiment_id", "run_id", "step", "timestamp_utc"],
        unique=False,
    )


def downgrade() -> None:
    """Drop every table and index."""
    op.drop_index("idx_metric_lookup", table_name="UnderlyingMetricTableEntry")
    op.drop_table("UnderlyingMetricTableEntry")
    op.drop_index(
        "idx_Page_shared_run_id",
        table_name="Page",
        sqlite_where=sa.text("owner_id IS NULL"),
        postgresql_where=sa.text("owner_id IS NULL"),
    )
    op.drop_index(
        "idx_Page_shared_project_id",
        table_name="Page",
        sqlite_where=sa.text("owner_id IS NULL"),
        postgresql_where=sa.text("owner_id IS NULL"),
    )
    op.drop_index(
        "idx_Page_shared_experiment_id",
        table_name="Page",
        sqlite_where=sa.text("owner_id IS NULL"),
        postgresql_where=sa.text("owner_id IS NULL"),
    )
    op.drop_table("Page")
    op.drop_table("HyperParams")
    op.drop_index("idx_artifact_lookup", table_name="Artifact")
    op.drop_table("Artifact")
    op.drop_table("Run")
    op.drop_table("Comment")
    op.drop_table("ProjectGrant")
    op.drop_table("Experiment")
    op.drop_table("Project")
    op.drop_table("PasswordCredential")
    op.drop_table("AuditLogEntry")
    op.drop_table("ArtifactPurgeTask")
    op.drop_table("ApiToken")
    op.drop_table("User")
    op.drop_table("AppState")
