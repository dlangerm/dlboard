"""Functions to use with a backing sqllite store."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, override

from structlog.stdlib import get_logger

from dltrack.serve._backend._data_store import set_data_store
from dltrack.serve._backend._sql_store_base import SQLStoreBase

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dash import Dash

    from dltrack import models


_log = get_logger(__name__)


class SQLLiteStore(SQLStoreBase[Path]):
    """Use a sqllite database as the data store."""

    def __init__(self, location: Path) -> None:
        """Initialize."""
        self._location = location
        location.parent.mkdir(parents=True, exist_ok=True)
        super().__init__()

    @override
    def _execute_raw_sql(
        self,
        statement: str,
        values: dict[str, Any] | None = None,
    ) -> Iterator[tuple[Any, ...]]:
        """Execute raw sql."""
        with sqlite3.connect(self._location) as conn:
            cur = conn.cursor()
            _log.info("Execute <%s> with values <%s>", statement, values)
            yield from cur.execute(statement, values or {}).fetchall()

    @override
    def _execute_raw_sql_query_many(
        self,
        statement: str,
        values: Iterable[dict[str, Any]] | None = None,
    ) -> Iterator[tuple[Any, ...]]:
        """Execute raw sql."""
        with sqlite3.connect(self._location) as conn:
            cur = conn.cursor()
            yield from cur.executemany(statement, values or []).fetchall()

    @classmethod
    def get_or_create(cls, loc: Path) -> SQLLiteStore:
        """Create."""
        return cls(location=loc)


def get_plugin(store_location: Path) -> models.PluginProtocol:
    """Get the underlying plugin."""

    class Plugin:
        @classmethod
        def plug(cls, app: Dash) -> None:
            """Plugin content."""
            set_data_store(app, SQLLiteStore.get_or_create(store_location))

    return Plugin
