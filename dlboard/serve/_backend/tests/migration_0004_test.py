"""Migration 0004 rebuilds the biggest tables' indexes, so how long it blocks them is part of what it promises."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from dlboard.conftest import StoreBackend
from dlboard.serve._backend._schema_upgrade import _MIGRATIONS_DIR
from dlboard.serve._backend.tests.migrations_test import schema_diff

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.postgres import PostgresSettings, PostgresStore

pytestmark = pytest.mark.postgres


@pytest.fixture
def store_backend() -> StoreBackend:
    return StoreBackend.POSTGRES


def test_upgrading_builds_every_index_before_dropping_the_ones_it_replaces(
    store: PostgresStore, postgres_settings: PostgresSettings
) -> None:
    """
    A drop holds its table's lock until the migration commits, so a build after it would wait with reads blocked.

    Downgrades the migrated database to 0003 and upgrades it again, recording the DDL the upgrade sends.
    """
    ddl: list[str] = []

    def record(_conn: sa.Connection, _cursor: object, statement: str, *_: object) -> None:
        ddl.append(statement.split("(")[0].strip())

    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    with store._engine.connect() as conn:
        conn.execute(sa.text(f'SET search_path TO "{postgres_settings.db_schema}"'))
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0003")
        sa.event.listen(conn, "before_cursor_execute", record)
        command.upgrade(cfg, "head")
        conn.execute(sa.text("RESET search_path"))
        conn.commit()

    creates = [i for i, statement in enumerate(ddl) if statement.startswith("CREATE INDEX")]
    drops = [i for i, statement in enumerate(ddl) if statement.startswith("DROP INDEX")]
    assert drops
    assert max(creates) < min(drops)
    assert schema_diff(store) == []
