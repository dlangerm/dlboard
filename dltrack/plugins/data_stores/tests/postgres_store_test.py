# pyright: reportPrivateUsage=false
"""
Tests for `PostgresStore`'s own settings and startup -- the shared store behavior is covered on
every backend by `dltrack/tests/sql_store_test.py` and `soft_delete_test.py`.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from dltrack import models
from dltrack.plugins.data_stores.postgres import PostgresSettings, PostgresStore


def test_settings_become_libpq_connect_args() -> None:
    settings = PostgresSettings(
        host="db1.internal,db2.internal",
        password=SecretStr("s3cret"),
        sslmode="verify-full",
        sslrootcert=Path("/etc/ssl/ca.pem"),
        target_session_attrs="read-write",
        statement_timeout_ms=5000,
        prepare_threshold=None,
        connect_args={"sslcrl": "/etc/ssl/crl.pem"},
    )

    assert settings.libpq_connect_args() == {
        "host": "db1.internal,db2.internal",
        "port": "5432",
        "dbname": "dltrack",
        "user": "dltrack",
        "password": "s3cret",
        "application_name": "dltrack",
        "sslmode": "verify-full",
        "sslrootcert": "/etc/ssl/ca.pem",
        "target_session_attrs": "read-write",
        "connect_timeout": "10",
        "options": "-c TimeZone=UTC -c statement_timeout=5000",
        "prepare_threshold": None,
        "sslcrl": "/etc/ssl/crl.pem",
    }
    assert "s3cret" not in repr(settings)


def test_settings_are_read_from_postgres_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret")
    monkeypatch.setenv("POSTGRES_POOL_SIZE", "20")
    monkeypatch.setenv("POSTGRES_CONNECT_ARGS", '{"sslcrl": "/etc/ssl/crl.pem"}')

    settings = PostgresSettings()

    assert (settings.host, settings.pool_size, settings.connect_args) == (
        "db.internal",
        20,
        {"sslcrl": "/etc/ssl/crl.pem"},
    )
    assert settings.password is not None
    assert settings.password.get_secret_value() == "s3cret"


@pytest.mark.postgres
def test_store_lives_in_its_schema_with_utc_sessions_and_no_password_in_the_engine(
    postgres_settings: PostgresSettings,
) -> None:
    store = PostgresStore(postgres_settings)
    try:
        with store._engine.connect() as conn:
            tables = set(sa.inspect(conn).get_table_names(schema=postgres_settings.db_schema))
            timezone = conn.execute(sa.text("SHOW TimeZone")).scalar_one()
        assert {table.name for table in store.tables.values()} <= tables
        assert timezone == "UTC"
        assert str(store._engine.url) == "postgresql+psycopg://"
    finally:
        store.dispose()


@pytest.mark.postgres
def test_workers_starting_at_once_against_a_fresh_schema_all_come_up(
    postgres_settings: PostgresSettings,
) -> None:
    """Every Granian worker creates the schema at startup; the advisory lock makes them take turns."""
    workers = 6
    barrier = threading.Barrier(workers)
    stores: list[PostgresStore] = []
    errors: list[Exception] = []

    def start() -> None:
        barrier.wait()
        try:
            stores.append(PostgresStore(postgres_settings))
        except Exception as error:  # noqa: BLE001 -- collected, then asserted on below
            errors.append(error)

    threads = [threading.Thread(target=start) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    try:
        assert errors == []
        assert stores[0].get_or_create_user("admin").scopes == [models.Scope.ALL]
    finally:
        for store in stores:
            store.dispose()
