"""
Schema migrations for the sqlite-backed data store.

Migrations are applied in order and tracked via SQLite's built-in `PRAGMA user_version`. Each
migration is a plain function that mutates a live `sqlite3.Connection`. The runner's contract is
hard-fail, not best-effort: a migration that raises for any reason other than "this exact change
was already applied by a racing process" propagates immediately, and `user_version` is only
advanced past a migration that returned without raising -- so a broken migration can never be
mistaken for a completed one.

Note SQLite (via Python's default "legacy" transaction handling) auto-commits each DDL statement
(`CREATE`/`ALTER`/`DROP TABLE`) as it runs, so a multi-statement migration can't be rolled back
atomically if it fails partway through -- there is no clean undo of the statements that already
ran. Migrations are written to tolerate this: every step they take is checked for "already applied"
before being (re-)applied (see `_table_has_foreign_keys`), so re-running a migration that partially
succeeded on a previous, failed attempt safely finishes the remaining work instead of redoing it or
corrupting state.
"""

from __future__ import annotations

import sqlite3
import typing

from structlog.stdlib import get_logger

from dltrack.serve import sql
from dltrack.serve._backend._sql_store_base import FOREIGN_KEYS, TABLES

if typing.TYPE_CHECKING:
    from pydantic import BaseModel

_log = get_logger(__name__)

MigrationFn = typing.Callable[[sqlite3.Connection], None]


def _table_has_foreign_keys(
    conn: sqlite3.Connection, table_name: str, foreign_keys: dict[str, type[BaseModel]]
) -> bool:
    """Whether `table_name` already has exactly the expected `FOREIGN KEY` constraints."""
    rows = conn.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
    # PRAGMA foreign_key_list columns: (id, seq, table, from, to, on_update, on_delete, match)
    existing = {(row[3], row[2]) for row in rows}
    expected = {(field_name, referenced.__name__) for field_name, referenced in foreign_keys.items()}
    return existing == expected


def _rebuild_table_with_foreign_keys(
    conn: sqlite3.Connection, model: type[BaseModel], foreign_keys: dict[str, type[BaseModel]]
) -> None:
    """
    Recreate `model`'s table in place with `foreign_keys` declared as real `FOREIGN KEY` clauses.

    SQLite has no `ALTER TABLE ... ADD CONSTRAINT`, so retrofitting a foreign key onto an existing
    table requires the documented rebuild procedure: create a new table with the desired schema,
    copy every row across, drop the old table, then rename the new one into place.
    """
    tmp_name = f"{model.__name__}__migrating"
    conn.execute(f"DROP TABLE IF EXISTS {tmp_name}")
    create_sql = sql.create_table_sql(model, foreign_keys).replace(
        f"CREATE TABLE IF NOT EXISTS {model.__name__}", f"CREATE TABLE {tmp_name}", 1
    )
    conn.execute(create_sql)
    columns = ",".join(model.model_fields.keys())
    conn.execute(f"INSERT INTO {tmp_name} ({columns}) SELECT {columns} FROM {model.__name__}")
    conn.execute(f"DROP TABLE {model.__name__}")
    conn.execute(f"ALTER TABLE {tmp_name} RENAME TO {model.__name__}")


def _migration_001_enforce_foreign_keys(conn: sqlite3.Connection) -> None:
    """Retrofit `FOREIGN KEY` constraints onto every table that has relation columns."""
    conn.execute("PRAGMA foreign_keys = OFF")
    for table in TABLES:
        foreign_keys = FOREIGN_KEYS.get(table)
        if not foreign_keys:
            continue
        if _table_has_foreign_keys(conn, table.__name__, foreign_keys):
            _log.debug("Table %s already has expected foreign keys, skipping rebuild", table.__name__)
            continue
        _log.info("Rebuilding table %s with foreign keys %s", table.__name__, list(foreign_keys))
        _rebuild_table_with_foreign_keys(conn, table, foreign_keys)

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        # Rows already orphaned before this migration ran (e.g. a project deleted by hand outside
        # dltrack) would otherwise silently end up under a constraint they don't actually satisfy.
        msg = f"Refusing to apply foreign keys: existing rows violate them: {violations}"
        raise RuntimeError(msg)
    conn.execute("PRAGMA foreign_keys = ON")


MIGRATIONS: list[tuple[int, MigrationFn]] = [
    (1, _migration_001_enforce_foreign_keys),
]


def _current_version(conn: sqlite3.Connection) -> int:
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    return int(version)


def run_migrations(conn: sqlite3.Connection, migrations: list[tuple[int, MigrationFn]] | None = None) -> None:
    """Bring `conn`'s schema up to the latest known version, hard-failing on any real error."""
    pending = sorted(
        (version, fn) for version, fn in (migrations or MIGRATIONS) if version > _current_version(conn)
    )
    for version, fn in pending:
        _log.info("Applying migration %s", version)
        try:
            fn(conn)
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                conn.rollback()
                raise
            _log.debug("Migration %s: change already present, treating as applied", version)
        except Exception:
            conn.rollback()
            raise
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
