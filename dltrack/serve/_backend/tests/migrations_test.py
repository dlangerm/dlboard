# pyright: reportPrivateUsage=false
"""
The contract the whole migration history has to hold once this is a real release: a fresh database
migrated through every revision in order must end up with exactly the schema the current models
describe -- not the schema some earlier `build_metadata()` call happened to produce. A gap here is
what let `0001_baseline.py` call `build_metadata()` live for as long as it did (see its own
docstring): there was nothing to fail if a revision stopped matching the code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from dltrack.conftest import EVERY_STORE_BACKEND, StoreBackend

if TYPE_CHECKING:
    from dltrack.serve._backend._sql_store_base import SQLStoreBase


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


def schema_diff(store: SQLStoreBase[Any]) -> list[Any]:
    """
    Every difference between `store`'s live schema and what `build_metadata()` describes today.

    `include_schemas=True` plus `version_table_schema` are what make this correct against a
    Postgres store scoped to its own non-default schema (`PostgresSettings.db_schema`): without
    them, autogenerate only ever looks at the connection's default schema, and (unrelatedly)
    flags Alembic's own `alembic_version` table as something `build_metadata()` is missing.
    `RESET search_path` undoes the one lasting side effect of `SQLStoreBase.__init__`'s schema-prep
    transaction, which sets it with a plain (session-scoped, not `LOCAL`) `SET` -- otherwise, a
    connection this test happens to reuse from the pool would reflect its *unqualified* tables as
    if they lived in that schema too, double-counting every one of them.
    """
    with store._engine.connect() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(sa.text("RESET search_path"))
        ctx = MigrationContext.configure(
            conn, opts={"include_schemas": True, "version_table_schema": store._metadata.schema}
        )
        return compare_metadata(ctx, store._metadata)


def test_migration_head_matches_the_models(store: SQLStoreBase[Any]) -> None:
    """
    A freshly migrated database has exactly the schema `build_metadata()` describes today.

    The `store` fixture already migrated to head by the time it's handed to the test. Any drift --
    a model field with no matching migration, a column type a revision got wrong, a renamed table --
    shows up here as a non-empty diff, pointing at exactly what's out of sync.
    """
    assert schema_diff(store) == []
