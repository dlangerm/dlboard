"""
Index each per-step table by how it is read and by its run, and index runs and hyperparameters by experiment.

`idx_metric_lookup` led with `key` and `idx_artifact_lookup` was every column but two, `experiment_id` fourth
among them. Every read of these tables is one experiment's, mostly with no key at all, so Postgres could not
seek into either: it read the whole table, every experiment's rows, on each call. Both now match
`idx_histogram_lookup`, `(experiment_id, key, run_id, step)` -- narrower than what they replace, so the
database gets smaller.

Nothing led with `run_id`, so deleting or purging a run (`ON DELETE CASCADE`, and the soft-delete and purge
queries) scanned each per-step table whole to find that run's rows. Each now has a `(run_id)` index.

`Run` gets `(experiment_id, created_at)`, so an experiment's runs come out newest first without a sort: "the newest
20" is every page load and live update. `HyperParams` had no index but its primary key, so every read of one
experiment's hyperparameters scanned every experiment's: it gets `(experiment_id, run_id)`.

On Postgres every index is built before any old one is dropped, and the rebuilt ones are built under a temporary
name and renamed over the old, so reads of the metric and artifact tables are never blocked by a build: building
an index blocks writes to its table (an experiment cannot log metrics meanwhile) but not reads, and only the drops
and renames at the end take a lock that blocks reads too, held until the migration commits. Even so, the builds
take as long as the tables are big: on a database with many millions of metric rows, upgrade at a quiet moment.

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
_BY_RUN = {
    "idx_metric_run": "UnderlyingMetricTableEntry",
    "idx_histogram_run": "UnderlyingHistogramTableEntry",
    "idx_artifact_run": "Artifact",
}
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
    """Build every new index, then swap the rebuilt ones in for the old."""
    renames = op.get_bind().dialect.name == "postgresql"
    for name, table in _PER_STEP.items():
        if not renames:
            # SQLite cannot rename an index, and locks its whole file for a write anyway.
            op.drop_index(name, table_name=table)
        op.create_index(f"{name}_new" if renames else name, table, _ACCESS_PATH, unique=False)
    for name, table in _BY_RUN.items():
        op.create_index(name, table, ["run_id"], unique=False)
    op.create_index("idx_run_experiment", "Run", ["experiment_id", "created_at"], unique=False)
    op.create_index("idx_hyperparams_experiment", "HyperParams", ["experiment_id", "run_id"], unique=False)
    if renames:
        for name, table in _PER_STEP.items():
            op.drop_index(name, table_name=table)
            op.execute(f'ALTER INDEX "{name}_new" RENAME TO "{name}"')


def downgrade() -> None:
    """Restore the indexes as the baseline built them."""
    op.drop_index("idx_hyperparams_experiment", table_name="HyperParams")
    op.drop_index("idx_run_experiment", table_name="Run")
    for name, table in _BY_RUN.items():
        op.drop_index(name, table_name=table)
    for name, table in _PER_STEP.items():
        op.drop_index(name, table_name=table)
        op.create_index(name, table, _BASELINE_COLUMNS[name], unique=False)
