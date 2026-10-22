"""
Index each per-step table by how it is read: one experiment, then a key, then a run's steps.

`idx_metric_lookup` led with `key` and `idx_artifact_lookup` was every column but two, `experiment_id` fourth
among them. Every read of these tables is one experiment's, mostly with no key at all, so Postgres could not
seek into either: it read the whole table, every experiment's rows, on each call. Both now match
`idx_histogram_lookup`, `(experiment_id, key, run_id, step)` -- narrower than what they replace, so the
database gets smaller -- and `Run` gets one on `experiment_id`, which the live-run checks of the key summaries
look runs up by.

No column or row changes: an index is only ever a faster way to the same rows. So a server still running the old
version keeps working against the upgraded database during a rolling upgrade, reading the rows the way it did
before (an older server cannot be started against it, as after any migration).
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
    """Rebuild the metric and artifact indexes on the access path, and index runs by experiment."""
    for name, table in _PER_STEP.items():
        op.drop_index(name, table_name=table)
        op.create_index(name, table, _ACCESS_PATH, unique=False)
    op.create_index("idx_run_experiment", "Run", ["experiment_id"], unique=False)


def downgrade() -> None:
    """Restore the indexes as the baseline built them."""
    op.drop_index("idx_run_experiment", table_name="Run")
    for name, table in _PER_STEP.items():
        op.drop_index(name, table_name=table)
        op.create_index(name, table, _BASELINE_COLUMNS[name], unique=False)
