# pyright: reportPrivateUsage=false
"""
Running migrations is the one new risk this adds over the old `create_all`: a second store,
constructed against a database a first one already brought to `head` (a worker restart, or a
second Postgres worker starting up concurrently), must upgrade to a no-op rather than fail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa

from dltrack.conftest import EVERY_STORE_BACKEND, StoreBackend
from dltrack.plugins.data_stores.postgres import PostgresSettings, PostgresStore
from dltrack.plugins.data_stores.sqlite import SQLLiteStore

if TYPE_CHECKING:
    from pathlib import Path

    from dltrack.serve._backend._sql_store_base import SQLStoreBase


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


def _restart(
    store_backend: StoreBackend, tmp_path: Path, postgres_settings: PostgresSettings
) -> SQLStoreBase[Any]:
    match store_backend:
        case StoreBackend.SQLITE:
            return SQLLiteStore(tmp_path / "test.sqlite")
        case StoreBackend.POSTGRES:
            return PostgresStore(postgres_settings)


def test_second_store_against_an_already_migrated_database_is_a_no_op(
    store_backend: StoreBackend, tmp_path: Path, postgres_settings: PostgresSettings
) -> None:
    first = _restart(store_backend, tmp_path, postgres_settings)
    second = _restart(store_backend, tmp_path, postgres_settings)
    try:
        ((revision,),) = second._execute(sa.text("select version_num from alembic_version"))
        assert revision == "0001"
        assert first.tables.keys() == second.tables.keys()
    finally:
        first.dispose()
        second.dispose()
