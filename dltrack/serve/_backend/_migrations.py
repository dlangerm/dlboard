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

import json
import sqlite3
import typing

import pendulum
from structlog.stdlib import get_logger

from dltrack import models
from dltrack._identity import resolve_username
from dltrack.serve import sql
from dltrack.serve._backend._app_state import APP_STATE_ROW_ID, AppState
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


def _existing_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


_PLACEHOLDER_DEFAULTS_BY_SQL_TYPE: dict[str, str] = {"TEXT": "''", "INTEGER": "0", "REAL": "0", "BLOB": "x''"}


def _rebuild_table_with_foreign_keys(
    conn: sqlite3.Connection, model: type[BaseModel], foreign_keys: dict[str, type[BaseModel]]
) -> None:
    """
    Recreate `model`'s table in place with `foreign_keys` declared as real `FOREIGN KEY` clauses.

    SQLite has no `ALTER TABLE ... ADD CONSTRAINT`, so retrofitting a foreign key onto an existing
    table requires the documented rebuild procedure: create a new table with the desired schema,
    copy every row across, drop the old table, then rename the new one into place.

    The new table is built from `model`'s *current* fields, which may already include columns the
    old table doesn't have yet (e.g. a field added to the model after this migration was written).
    Only columns present in the old table are copied across; any newer column is left unset for
    existing rows. A newer column that's `NOT NULL` (nothing here makes fields nullable just to ease
    a migration -- see e.g. `Project.created_at`) gets a type-appropriate placeholder `DEFAULT` so
    the rebuild itself stays valid; whichever later migration actually owns introducing that column
    is responsible for overwriting the placeholder with real data. This keeps a fixed, already-shipped
    migration correct indefinitely as the models it touches keep evolving, instead of only working
    for the exact model shape it was authored against.
    """
    existing = _existing_columns(conn, model.__name__)
    new_required_columns = {
        field_name: sql.annotation_to_sqltype(field.annotation)  # pyright: ignore[reportArgumentType]
        for field_name, field in model.model_fields.items()
        if field_name not in existing and field_name != sql.ID_KEY
    }
    column_defaults = {
        field_name: _PLACEHOLDER_DEFAULTS_BY_SQL_TYPE[sql_type.split()[0]]
        for field_name, sql_type in new_required_columns.items()
        if "NOT NULL" in sql_type
    }

    tmp_name = f"{model.__name__}__migrating"
    conn.execute(f"DROP TABLE IF EXISTS {tmp_name}")
    create_sql = sql.create_table_sql(model, foreign_keys, column_defaults=column_defaults).replace(
        f"CREATE TABLE IF NOT EXISTS {model.__name__}", f"CREATE TABLE {tmp_name}", 1
    )
    conn.execute(create_sql)
    shared_columns = ",".join(c for c in model.model_fields if c in existing)
    conn.execute(f"INSERT INTO {tmp_name} ({shared_columns}) SELECT {shared_columns} FROM {model.__name__}")
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


def _add_column_if_missing(
    conn: sqlite3.Connection, table_name: str, column_name: str, column_sql: str
) -> None:
    if column_name in _existing_columns(conn, table_name):
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")


def _claim_bootstrap_admin(conn: sqlite3.Connection) -> int:
    """
    Ensure a bootstrap admin user exists and, at most once ever, grant it `Scope.ALL`.

    Mirrors `SQLStoreBase.get_or_create_user`'s claim logic (a single `UPDATE ... WHERE
    bootstrap_admin_assigned = 0` is atomic under SQLite's serialized writes, so two processes
    racing this at once can never both win), but operates on a raw connection since migrations run
    outside that abstraction. Returns the user's id, to attribute pre-existing rows to.
    """
    username = resolve_username()
    conn.execute(
        f"INSERT OR IGNORE INTO {models.User.__name__} (username, scopes, created_at) "
        "VALUES (:username, '[]', :created_at)",
        {"username": username, "created_at": pendulum.now(pendulum.UTC).isoformat()},
    )
    (user_id,) = conn.execute(
        f"SELECT id FROM {models.User.__name__} WHERE username = :username",
        {"username": username},
    ).fetchone()

    claimed = conn.execute(
        f"UPDATE {AppState.__name__} SET bootstrap_admin_assigned = 1 "
        f"WHERE id = {APP_STATE_ROW_ID} AND bootstrap_admin_assigned = 0 RETURNING id"
    ).fetchall()
    if claimed:
        _log.info("Granting bootstrap admin scopes to user %s (%s)", user_id, username)
        conn.execute(
            f"UPDATE {models.User.__name__} SET scopes = :scopes WHERE id = :id",
            {"scopes": json.dumps([models.Scope.ALL.value]), "id": user_id},
        )
    return int(user_id)


# `created_at` is genuinely never null once a row exists (the model field is a plain
# `AwareDatetime`, not `AwareDatetime | None`) -- adding the column with `NOT NULL DEFAULT
# <placeholder>` (a documented, SQLite-supported ADD COLUMN pattern for constant defaults) keeps
# that invariant true at the SQL level too. The placeholder is only ever visible for the instant
# between this ALTER TABLE and the backfill UPDATE a few lines below, which unconditionally
# overwrites it with a real timestamp for every legacy row.
_CREATED_AT_BRIDGE_PLACEHOLDER = "1970-01-01T00:00:00+00:00"


def _migration_002_add_provenance_columns(conn: sqlite3.Connection) -> None:
    """Add `created_by`/`created_at` to the user-deletable entity tables and backfill old rows."""
    tables = (models.Project, models.Experiment, models.Run, models.Artifact)
    for table in tables:
        _add_column_if_missing(
            conn, table.__name__, "created_by", f"created_by INTEGER REFERENCES {models.User.__name__}(id)"
        )
        _add_column_if_missing(
            conn,
            table.__name__,
            "created_at",
            f"created_at TEXT NOT NULL DEFAULT '{_CREATED_AT_BRIDGE_PLACEHOLDER}'",
        )

    needs_backfill = any(
        conn.execute(f"SELECT 1 FROM {table.__name__} WHERE created_by IS NULL LIMIT 1").fetchone()
        for table in tables
    )
    if not needs_backfill:
        return

    bootstrap_user_id = _claim_bootstrap_admin(conn)
    now = pendulum.now(pendulum.UTC).isoformat()
    for table in tables:
        # `created_by IS NULL` identifies rows that predate this migration -- their `created_at` is
        # therefore always still the bridge placeholder, safe to overwrite unconditionally.
        conn.execute(
            f"UPDATE {table.__name__} SET created_by = :user_id, created_at = :now WHERE created_by IS NULL",
            {"user_id": bootstrap_user_id, "now": now},
        )


def _migration_003_add_soft_delete_columns(conn: sqlite3.Connection) -> None:
    """
    Add `deleted_at`/`deleted_by` to the soft-deletable entity tables.

    Unlike `created_by`/`created_at` in migration 2, no backfill is needed: NULL is already the
    semantically correct value for every pre-existing row ("not deleted"), so a plain nullable
    `ALTER TABLE ... ADD COLUMN` is sufficient.
    """
    for table in (models.Project, models.Experiment, models.Run, models.Artifact):
        _add_column_if_missing(
            conn, table.__name__, "deleted_by", f"deleted_by INTEGER REFERENCES {models.User.__name__}(id)"
        )
        _add_column_if_missing(conn, table.__name__, "deleted_at", "deleted_at TEXT")


MIGRATIONS: list[tuple[int, MigrationFn]] = [
    (1, _migration_001_enforce_foreign_keys),
    (2, _migration_002_add_provenance_columns),
    (3, _migration_003_add_soft_delete_columns),
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
