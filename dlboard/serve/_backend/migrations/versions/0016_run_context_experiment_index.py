"""
An index on the run contexts of an experiment.

Whether any of an experiment's runs recorded a context (`has_run_contexts`) is what offers a chart of them, and nothing
led with `experiment_id`: the probe read every experiment's contexts. One new index, so every existing row is untouched:
expand-only, per the policy in `docs/compatibility.md`. An older server only reads it as it reads the table.
"""

from __future__ import annotations

from alembic import op  # pyrefly: ignore [implicit-reexport]

# revision identifiers, used by Alembic.
revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the index."""
    op.create_index("idx_RunContext_experiment", "RunContext", ["experiment_id", "run_id"], unique=False)


def downgrade() -> None:
    """Drop it again."""
    op.drop_index("idx_RunContext_experiment", table_name="RunContext")
