"""Functions to use with a backing sqllite store."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, override

import sqlalchemy as sa
from pydantic_settings import BaseSettings
from sqlalchemy import event
from sqlalchemy.dialects import sqlite
from sqlalchemy.pool import NullPool

from dltrack.serve import SCHEMA_PREP_OPTION, SQLStoreBase, set_data_store

if TYPE_CHECKING:
    import sqlite3

    from dash import Dash


DEFAULT_BUSY_TIMEOUT_MS: int = 30_000


def _engine(location: Path, busy_timeout_ms: int) -> sa.Engine:
    """
    An engine whose every connection is configured for concurrent access.

    WAL journal mode lets readers proceed while a writer holds the write lock (the default
    rollback-journal mode locks the whole file for the duration of a write), which is where most
    "database is locked" errors under concurrent client flush load actually come from -- readers
    vs. the one writer, not writer vs. writer. `timeout` covers the writer-vs-writer case that's
    left: it sets sqlite's own busy handler, which retries (with its own internal backoff) for up
    to that long before raising `OperationalError`, instead of us hand-rolling a Python-side retry
    loop around every call. `NullPool`: a sqlite connection is just an open file, so each
    transaction opens its own rather than holding a pool of them open across threads.
    """
    engine = sa.create_engine(
        f"sqlite:///{location}",
        poolclass=NullPool,
        connect_args={"timeout": busy_timeout_ms / 1000, "check_same_thread": False},
    )

    event.listen(engine, "connect", _configure_connection)
    event.listen(engine, "begin", _begin)
    return engine


def _configure_connection(dbapi_connection: sqlite3.Connection, _record: object) -> None:
    dbapi_connection.execute("PRAGMA foreign_keys = ON")
    dbapi_connection.execute("PRAGMA journal_mode = WAL")
    # pysqlite's own implicit transaction handling defers the real `BEGIN` until the first write
    # statement, which is both generally broken for DDL and (see `_begin`) is specifically how two
    # connections can both run a read (e.g. "does this table exist yet?") before either actually
    # holds a lock, race past each other, and both decide to create it. Disabling it here (and
    # emitting our own explicit `BEGIN` below, in the `begin` hook) fixes both: the lock is taken
    # at a point we choose, not at an implicit first write.
    dbapi_connection.isolation_level = None


def _begin(conn: sa.Connection) -> None:
    """
    Explicitly `BEGIN` every transaction (see `_configure_connection`), deferred except one.

    Deferred (the default) only takes sqlite's write-intent lock at the first write, which is what
    lets WAL readers proceed concurrently with a writer -- see `_engine`. The one exception is
    `SQLStoreBase.__init__`'s schema creation/migration transaction (flagged via
    `SCHEMA_PREP_OPTION`, since several processes can start that one concurrently against a brand
    new database): that one takes the lock immediately, before its own first read, so a second
    connection doing the same waits (via the busy handler `_engine`'s `timeout` configures) rather
    than racing it. Forcing that onto every ordinary query too would serialize all of them behind
    this one lock for the engine's whole lifetime, throwing away the WAL concurrency above.
    """
    mode = "IMMEDIATE" if conn.get_execution_options().get(SCHEMA_PREP_OPTION) else "DEFERRED"
    conn.exec_driver_sql(f"BEGIN {mode}")


class SQLLiteStore(SQLStoreBase[Path]):
    """Use a sqllite database as the data store."""

    def __init__(self, location: Path, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> None:
        """Initialize."""
        self._location = location
        self._busy_timeout_ms = busy_timeout_ms
        location.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(_engine(location, busy_timeout_ms))

    @override
    def _insert_ignoring_conflicts(self, table: sa.Table) -> sa.Insert:
        return sqlite.insert(table).on_conflict_do_nothing()

    @classmethod
    def get_or_create(cls, loc: Path, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> SQLLiteStore:
        """Create."""
        return cls(location=loc, busy_timeout_ms=busy_timeout_ms)


class AppSettings(BaseSettings):
    """Environment variables."""

    sqlite_location: Path = Path.home() / ".dltrack.sqlite"
    sqlite_busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
    """How long sqlite's own busy handler retries a locked database before raising `OperationalError`."""


def plug(app: Dash) -> None:
    """Plugin content."""
    env = AppSettings()
    set_data_store(app, SQLLiteStore.get_or_create(env.sqlite_location, env.sqlite_busy_timeout_ms))
