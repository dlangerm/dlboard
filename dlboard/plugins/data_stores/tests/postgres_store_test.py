"""
Tests for `PostgresStore`'s own settings and startup -- the shared store behavior is covered on
every backend by `dlboard/tests/sql_store_test.py` and `soft_delete_test.py`.
"""

from __future__ import annotations

import threading
from pathlib import Path

import psycopg
import pytest
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs

from dlboard import models
from dlboard.plugins.data_stores.postgres import PostgresSettings, PostgresStore, _unreachable


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
        "dbname": "dlboard",
        "user": "dlboard",
        "password": "s3cret",
        "application_name": "dlboard",
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
    monkeypatch.setenv("DLBOARD_POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("DLBOARD_POSTGRES_PASSWORD", "s3cret")
    monkeypatch.setenv("DLBOARD_POSTGRES_POOL_SIZE", "20")
    monkeypatch.setenv("DLBOARD_POSTGRES_CONNECT_ARGS", '{"sslcrl": "/etc/ssl/crl.pem"}')

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
            timezone: str = conn.execute(sa.text("SHOW TimeZone")).scalar_one()
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
        assert stores[0].get_or_create_user(models.Principal.unverified("admin")).scopes == [models.Scope.ALL]
    finally:
        for store in stores:
            store.dispose()


@pytest.mark.parametrize(
    ("message", "waiting_can_fix_it"),
    [
        ("failed to resolve host 'postgres': [Errno -2] Name or service not known", True),
        (
            'connection failed: connection to server at "127.0.0.1", port 5432 failed: Connection refused',
            True,
        ),
        (
            'connection failed: connection to server at "172.23.0.4", port 5432 failed: server closed the connection',
            True,
        ),
        (
            'connection failed: connection to server at "172.23.0.4", port 5432 failed: FATAL:  the database system is starting up',
            True,
        ),
        (
            'connection failed: connection to server at "172.23.0.4", port 5432 failed: FATAL:  the database system is shutting down',
            True,
        ),
        (
            'connection failed: connection to server at "127.0.0.1", port 5432 failed: FATAL:  password authentication failed for user "dlboard"',
            False,
        ),
        (
            'connection failed: connection to server at "127.0.0.1", port 5432 failed: FATAL:  database "nodb" does not exist',
            False,
        ),
    ],
)
def test_only_a_server_that_is_not_there_yet_is_worth_waiting_for(
    message: str, waiting_can_fix_it: bool
) -> None:
    error = OperationalError("connect", {}, psycopg.OperationalError(message))

    assert _unreachable(error) is waiting_can_fix_it


def test_a_server_that_cannot_be_reached_is_retried_for_the_startup_wait_and_then_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DLBOARD_STARTUP_WAIT_S", "0.5")

    with capture_logs() as logs, pytest.raises(OperationalError, match="failed to resolve host"):
        PostgresStore(PostgresSettings(host="postgres.invalid", connect_timeout_s=1))

    assert [event["event"] for event in logs if event["log_level"] == "warning"] == [
        "database not reachable yet; retrying"
    ]


@pytest.mark.postgres
def test_a_wrong_password_is_raised_at_once_however_long_the_startup_wait(
    postgres_settings: PostgresSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLBOARD_STARTUP_WAIT_S", "600")
    wrong = postgres_settings.model_copy(update={"password": SecretStr("not-the-password")})

    with capture_logs() as logs, pytest.raises(OperationalError, match="password authentication failed"):
        PostgresStore(wrong)

    assert [event for event in logs if event["log_level"] == "warning"] == []
