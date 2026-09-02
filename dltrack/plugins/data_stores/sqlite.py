"""Functions to use with a backing sqllite store."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, override

from pydantic_settings import BaseSettings
from structlog.stdlib import get_logger

from dltrack.serve import SQLStoreBase, set_data_store

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dash import Dash


_log = get_logger(__name__)

DEFAULT_BUSY_TIMEOUT_MS: int = 30_000


class SQLLiteStore(SQLStoreBase[Path]):
    """Use a sqllite database as the data store."""

    def __init__(self, location: Path, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> None:
        """Initialize."""
        self._location = location
        self._busy_timeout_ms = busy_timeout_ms
        location.parent.mkdir(parents=True, exist_ok=True)
        super().__init__()

    def _connect(self) -> sqlite3.Connection:
        """
        Open a connection configured for concurrent access.

        WAL journal mode lets readers proceed while a writer holds the write lock (the default
        rollback-journal mode locks the whole file for the duration of a write), which is where most
        "database is locked" errors under concurrent client flush load actually come from -- readers
        vs. the one writer, not writer vs. writer. `timeout` covers the writer-vs-writer case that's
        left: it sets sqlite's own busy handler, which retries (with its own internal backoff) for up
        to that long before raising `OperationalError`, instead of us hand-rolling a Python-side retry
        loop around every call.
        """
        conn = sqlite3.connect(self._location, timeout=self._busy_timeout_ms / 1000)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @override
    def _execute_raw_sql(
        self,
        statement: str,
        values: dict[str, Any] | None = None,
    ) -> Iterator[tuple[Any, ...]]:
        """Execute raw sql."""
        with self._connect() as conn:
            cur = conn.cursor()
            _log.debug("Execute <%s> with values <%s>", statement, values)
            yield from cur.execute(statement, values or {}).fetchall()

    @override
    def _execute_raw_sql_query_many(
        self,
        statement: str,
        values: Iterable[dict[str, Any]] | None = None,
    ) -> Iterator[tuple[Any, ...]]:
        """Execute raw sql."""
        with self._connect() as conn:
            cur = conn.cursor()
            yield from cur.executemany(statement, values or []).fetchall()

    @override
    def _execute_in_transaction(
        self, statements: Iterable[tuple[str, dict[str, Any] | None]]
    ) -> list[list[tuple[Any, ...]]]:
        """Execute every statement on one connection, committing only if all of them succeed."""
        results: list[list[tuple[Any, ...]]] = []
        with self._connect() as conn:
            for statement, values in statements:
                _log.debug("Execute (in transaction) <%s> with values <%s>", statement, values)
                results.append(conn.execute(statement, values or {}).fetchall())
        return results

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
