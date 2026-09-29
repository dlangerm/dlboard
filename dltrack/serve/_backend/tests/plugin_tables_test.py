"""
A plugin's own tables in the store's database: declared on `metadata`, created by `create_tables`,
owned by the store's rows -- using a backend-specific column type where the backend has one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from dltrack import models
from dltrack.conftest import EVERY_STORE_BACKEND, StoreBackend, create_entity_chain
from dltrack.plugins import LOCAL_STORAGE
from dltrack.serve import SqlDialect, get_sql_store
from dltrack.serve import app as build_app

if TYPE_CHECKING:
    from pathlib import Path

    from dltrack.serve import SQLStoreBase


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


def _declare_embeddings(store: SQLStoreBase[Any]) -> sa.Table:
    """What an embeddings plugin would declare: a vector per run -- a native array where there is one."""
    vector: sa.types.TypeEngine[Any]
    match store.dialect:
        case SqlDialect.POSTGRES:
            vector = postgresql.ARRAY(sa.Double[float]())
        case SqlDialect.SQLITE:
            vector = sa.JSON()
    return sa.Table(
        "RunEmbedding",
        store.metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "run_id",
            sa.BigInteger,
            sa.ForeignKey(store.tables[models.Run].c.id, ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("vector", vector, nullable=False),
    )


def test_a_plugin_table_is_created_idempotently_and_purged_with_its_run(
    store: SQLStoreBase[Any], admin: models.User
) -> None:
    chain = create_entity_chain(store)
    embeddings = _declare_embeddings(store)
    store.create_tables(embeddings)
    store.create_tables(embeddings)  # every worker process does this at startup
    with store.engine.begin() as conn:
        conn.execute(sa.insert(embeddings).values(run_id=chain.run_id, vector=[0.25, 0.5]))
        assert conn.execute(sa.select(embeddings.c.vector)).scalar_one() == [0.25, 0.5]

    store.delete_run(chain.run_id, admin)
    store.purge_run(chain.run_id, admin)

    with store.engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(embeddings)).scalar_one() == 0


def test_the_sqlite_storage_plugin_publishes_its_sql_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLITE_LOCATION", str(tmp_path / "test.sqlite"))
    monkeypatch.setenv("ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))

    app = build_app(LOCAL_STORAGE)

    assert get_sql_store(app).dialect is SqlDialect.SQLITE
