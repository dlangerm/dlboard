"""
Index each per-step table by how it is read, and index runs and hyperparameters by experiment.

`idx_metric_lookup` led with `key` and `idx_artifact_lookup` was every column but two, `experiment_id` fourth
among them. Every read of these tables is one experiment's, mostly with no key at all, so Postgres could not
seek into either: it read the whole table, every experiment's rows, on each call. Both now match
`idx_histogram_lookup`, `(experiment_id, key, run_id, step)` -- narrower than what they replace, so the
database gets smaller.

`Run` gets `(experiment_id, created_at)`, so an experiment's runs come out newest first without a sort: "the newest
20" is every page load and live update. `HyperParams` had no index but its primary key, so every read of one
experiment's hyperparameters scanned every experiment's: it gets `(experiment_id, run_id)`.

Rebuilding the metric and artifact indexes drops each one and creates it again in one transaction, so the drop's lock
on its table is held until the create commits: reads and writes of those tables wait for the rebuild. Creating the
`Run` and `HyperParams` indexes only blocks writes to those tables while it runs. Upgrade at a quiet moment; on a
database with many millions of metric rows the wait is as long as the rebuild.

No column or row changes: an index is only ever a faster way to the same rows. So once the migration has committed,
a server still running the old version keeps working against the upgraded database during a rolling upgrade,
reading the rows the way it did before (an older server cannot be started against it, as after any migration).
"""

from __future__ import annotations

from alembic import op  # pyrefly: ignore [implicit-reexport]

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_PER_STEP = {
    "idx_metric_lookup": "UnderlyingMetricTableEntry",
    "idx_artifact_lookup": "Artifact",
}
_ACCESS_PATH = ["experiment_id", "key", "run_id", "step"]
_BASELINE_COLUMNS = {
    "idx_metric_lookup": ["key", "experiment_id", "run_id", "step", "timestamp_utc"],
    "idx_artifact_lookup": [
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
}


def upgrade() -> None:
    """Rebuild the metric and artifact indexes on the access path, and index runs and hyperparameters by experiment."""
    for name, table in _PER_STEP.items():
        op.drop_index(name, table_name=table)
        op.create_index(name, table, _ACCESS_PATH, unique=False)
    op.create_index("idx_run_experiment", "Run", ["experiment_id", "created_at"], unique=False)
    op.create_index("idx_hyperparams_experiment", "HyperParams", ["experiment_id", "run_id"], unique=False)


def downgrade() -> None:
    """Restore the indexes as the baseline built them."""
    op.drop_index("idx_hyperparams_experiment", table_name="HyperParams")
    op.drop_index("idx_run_experiment", table_name="Run")
    for name, table in _PER_STEP.items():
        op.drop_index(name, table_name=table)
        op.create_index(name, table, _BASELINE_COLUMNS[name], unique=False)
