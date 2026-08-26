"""Functions to use with a backing sqllite store."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, override

from pydantic_settings import BaseSettings
from structlog.stdlib import get_logger

from dltrack.serve import SQLStoreBase, run_migrations, set_data_store

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dash import Dash


_log = get_logger(__name__)


class SQLLiteStore(SQLStoreBase[Path]):
    """Use a sqllite database as the data store."""

    def __init__(self, location: Path) -> None:
        """Initialize."""
        self._location = location
        location.parent.mkdir(parents=True, exist_ok=True)
        super().__init__()

    @override
    def _run_migrations(self) -> None:
        """Run migrations. SQLite requires the pragma to be set per-connection, not once globally."""
        with sqlite3.connect(self._location) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            run_migrations(conn)

    @override
    def _execute_raw_sql(
        self,
        statement: str,
        values: dict[str, Any] | None = None,
    ) -> Iterator[tuple[Any, ...]]:
        """Execute raw sql."""
        with sqlite3.connect(self._location) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
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
        with sqlite3.connect(self._location) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            cur = conn.cursor()
            yield from cur.executemany(statement, values or []).fetchall()

    @override
    def _execute_in_transaction(
        self, statements: Iterable[tuple[str, dict[str, Any] | None]]
    ) -> list[list[tuple[Any, ...]]]:
        """Execute every statement on one connection, committing only if all of them succeed."""
        results: list[list[tuple[Any, ...]]] = []
        with sqlite3.connect(self._location) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            for statement, values in statements:
                _log.debug("Execute (in transaction) <%s> with values <%s>", statement, values)
                results.append(conn.execute(statement, values or {}).fetchall())
        return results

    @classmethod
    def get_or_create(cls, loc: Path) -> SQLLiteStore:
        """Create."""
        return cls(location=loc)


class AppSettings(BaseSettings):
    """Environment variables."""

    sqlite_location: Path = Path.home() / ".dltrack.sqlite"


def plug(app: Dash) -> None:
    """Plugin content."""
    env = AppSettings()
    set_data_store(app, SQLLiteStore.get_or_create(env.sqlite_location))
