"""
The per-step tables carry exactly the indexes their reads need, and no more.

Every logged step is a row in one of these tables, so every index on them is written on every insert: an index added to
speed up some read slows ingest for every run. `query_plans_test.py` and `read_scaling_test.py` guard the read side; this
guards the write side. Changing `_BUDGET` is a deliberate decision, and should come with an ingest measurement (logging a
batch of metrics before and after, on SQLite) in the PR that changes it.

It reads the migrated database, not `build_metadata()`, on every backend: a migration could create an index the models
don't describe, or a partial index without its `WHERE`, which `migrations_test.py`'s autogenerate diff does not compare.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

import pytest
import sqlalchemy as sa

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend

if TYPE_CHECKING:
    from dlboard.serve._backend._sql_store_base import SQLStoreBase


class Index(NamedTuple):
    """One index as the database holds it: the primary key and unique constraints count too, being written alike."""

    name: str
    columns: tuple[str, ...]
    where: str | None = None


_LOOKUP = ("experiment_id", "key", "run_id", "step")
"""The access path every read of a per-step table seeks through (see `query_plans_test.py`)."""
_BUDGET: dict[type[Any], frozenset[Index]] = {
    models.UnderlyingMetricTableEntry: frozenset(
        {
            Index("primary key", ("id",)),
            Index("idx_metric_lookup", _LOOKUP),
            Index("idx_metric_run", ("run_id",)),
        }
    ),
    models.UnderlyingHistogramTableEntry: frozenset(
        {
            Index("primary key", ("id",)),
            Index("idx_histogram_lookup", _LOOKUP),
            Index("idx_histogram_run", ("run_id",)),
        }
    ),
    # Partial: only what is in the Trash, so nearly free on ingest.
    models.Artifact: frozenset(
        {
            Index("primary key", ("id",)),
            Index("idx_artifact_lookup", _LOOKUP),
            Index("idx_artifact_run", ("run_id",)),
            Index("idx_Artifact_deleted", ("deleted_at",), where="deleted_at IS NOT NULL"),
        }
    ),
}
"""
Each per-step table's indexes: the lookup every read seeks through, `run_id` so deleting or purging a run doesn't scan, and
the primary key.
"""


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


@pytest.mark.parametrize("model", _BUDGET, ids=lambda m: m.__name__)
def test_a_per_step_table_has_only_its_budgeted_indexes(store: SQLStoreBase[Any], model: type[Any]) -> None:
    inspector, schema = sa.inspect(store._engine), store._metadata.schema
    indexes = {
        Index(
            str(index["name"]),
            tuple(str(c) for c in index["column_names"]),
            # SQLite keeps the `WHERE` as written, Postgres in parentheses.
            next(
                (str(w).strip("()") for k, w in index["dialect_options"].items() if k.endswith("_where")),
                None,
            ),
        )
        for index in inspector.get_indexes(model.__name__, schema=schema)
    }
    unique = {
        Index(str(u["name"]), tuple(u["column_names"]))
        for u in inspector.get_unique_constraints(model.__name__, schema)
    }
    primary_key = Index(
        "primary key", tuple(inspector.get_pk_constraint(model.__name__, schema)["constrained_columns"])
    )

    assert indexes | unique | {primary_key} == _BUDGET[model]
