"""
Index the runs and hyperparameters of an experiment the way the experiment page reads them.

`HyperParams` had no index but its primary key, so reading one experiment's hyperparameters (the Runs table, the
live poll, and the 20 runs a chart draws) scanned every experiment's: a table with a row per run, so the more runs
a database held anywhere, the slower every page. It now has `(experiment_id, run_id)`.

`idx_run_experiment` found an experiment's runs but not in order, so "the newest 20" (every page load and live
update) read all of them to sort. It now leads with `experiment_id` and follows with `created_at`, which the
newest-first order reads straight off.

Indexes only: no column or row changes, so a server still running the old version keeps working against the
upgraded database during a rolling upgrade, reading the rows the way it did before.
"""

from __future__ import annotations

from alembic import op  # pyrefly: ignore [implicit-reexport]

# revision identifiers, used by Alembic.
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Order an experiment's runs by when they were created, and index its hyperparameters."""
    op.drop_index("idx_run_experiment", table_name="Run")
    op.create_index("idx_run_experiment", "Run", ["experiment_id", "created_at"], unique=False)
    op.create_index("idx_hyperparams_experiment", "HyperParams", ["experiment_id", "run_id"], unique=False)


def downgrade() -> None:
    """Back to runs indexed by experiment alone, and hyperparameters by nothing."""
    op.drop_index("idx_hyperparams_experiment", table_name="HyperParams")
    op.drop_index("idx_run_experiment", table_name="Run")
    op.create_index("idx_run_experiment", "Run", ["experiment_id"], unique=False)
