# pyright: reportPrivateUsage=false
"""Tests for the schema migration runner in `_migrations.py`.

Builds a version-0 schema by hand -- the table shapes as they were before any migration in this
module existed -- and runs the real migration chain against it, so a broken migration fails the
test suite loudly rather than only failing silently against someone's production database.

The v0 fixture is deliberately *hardcoded* SQL, not derived from the live `dltrack.models`
definitions: those models keep gaining fields as new migrations are added to backfill them, so
reflecting off the live models would silently stop being "version 0" the moment a model changes.
A frozen fixture is what makes this a real regression test of the migration chain instead of a
test that only ever exercises whatever the latest schema already looks like.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

from dltrack.serve._backend import _migrations

if TYPE_CHECKING:
    from pathlib import Path


def _v0_connection(db_path: Path) -> sqlite3.Connection:
    """
    A connection to a database with the original schema: no foreign keys on the entity tables.

    `User` and `AppState` are included (seeded, unclaimed) even though this represents "before any
    migration ran": in real usage `SQLStoreBase.__init__` always creates every currently-known
    table via `CREATE TABLE IF NOT EXISTS` -- including wholly new ones -- *before* running
    migrations, so by the time a migration function actually executes, brand new tables it depends
    on are guaranteed to already exist. Migrations only need to handle *changes* to tables that
    already existed, never the both-at-once case of a missing table it also has to populate.
    """
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE User (
            username TEXT NOT NULL, scopes TEXT NOT NULL, created_at TEXT NOT NULL,
            id INTEGER PRIMARY KEY AUTOINCREMENT, UNIQUE (username)
        );
        CREATE TABLE AppState (id INTEGER PRIMARY KEY, bootstrap_admin_assigned INTEGER NOT NULL);
        INSERT INTO AppState (id, bootstrap_admin_assigned) VALUES (1, 0);
        CREATE TABLE Project (
            name TEXT NOT NULL, description TEXT NOT NULL, id INTEGER PRIMARY KEY AUTOINCREMENT
        );
        CREATE TABLE Experiment (
            project_id INTEGER NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL,
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );
        CREATE TABLE Run (experiment_id INTEGER NOT NULL, id INTEGER PRIMARY KEY AUTOINCREMENT);
        CREATE TABLE UnderlyingMetricTableEntry (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, value REAL,
            experiment_id INTEGER NOT NULL, run_id INTEGER NOT NULL, step INTEGER NOT NULL,
            timestamp_utc TEXT NOT NULL
        );
        CREATE TABLE HyperParams (
            run_id INTEGER NOT NULL, experiment_id INTEGER NOT NULL, raw_hparams TEXT NOT NULL,
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );
        CREATE TABLE Artifact (
            key TEXT NOT NULL, fname TEXT NOT NULL, tags TEXT NOT NULL, run_id INTEGER NOT NULL,
            experiment_id INTEGER NOT NULL, step INTEGER, id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref TEXT NOT NULL
        );
        CREATE TABLE Page (
            run_id INTEGER, experiment_id INTEGER, project_id INTEGER, panels TEXT NOT NULL,
            page_settings TEXT NOT NULL, id INTEGER PRIMARY KEY AUTOINCREMENT
        );
    """)
    conn.commit()
    return conn


def _insert_project(conn: sqlite3.Connection, *, name: str = "p") -> int:
    cur = conn.execute("INSERT INTO Project (name, description) VALUES (?, ?)", (name, ""))
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def _insert_experiment(conn: sqlite3.Connection, project_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO Experiment (project_id, name, description) VALUES (?, ?, ?)", (project_id, "", "")
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def _insert_run(conn: sqlite3.Connection, experiment_id: int) -> int:
    cur = conn.execute("INSERT INTO Run (experiment_id) VALUES (?)", (experiment_id,))
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_migration_from_v0_adds_foreign_keys_and_preserves_data(tmp_path: Path) -> None:
    conn = _v0_connection(tmp_path / "v0.sqlite")
    project_id = _insert_project(conn, name="keep-me")
    experiment_id = _insert_experiment(conn, project_id)
    _insert_run(conn, experiment_id)

    _migrations.run_migrations(conn)

    experiment_fks = {(row[3], row[2]) for row in conn.execute("PRAGMA foreign_key_list(Experiment)")}
    assert experiment_fks == {("project_id", "Project"), ("created_by", "User"), ("deleted_by", "User")}

    run_fks = {(row[3], row[2]) for row in conn.execute("PRAGMA foreign_key_list(Run)")}
    assert run_fks == {("experiment_id", "Experiment"), ("created_by", "User"), ("deleted_by", "User")}

    metric_fks = {
        (row[3], row[2]) for row in conn.execute("PRAGMA foreign_key_list(UnderlyingMetricTableEntry)")
    }
    assert metric_fks == {("experiment_id", "Experiment"), ("run_id", "Run")}

    assert conn.execute("SELECT name FROM Project WHERE id = ?", (project_id,)).fetchone() == ("keep-me",)
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == max(v for v, _ in _migrations.MIGRATIONS)


def test_migration_backfills_provenance_columns_to_a_resolved_bootstrap_admin(tmp_path: Path) -> None:
    conn = _v0_connection(tmp_path / "provenance.sqlite")
    project_id = _insert_project(conn)
    experiment_id = _insert_experiment(conn, project_id)
    _insert_run(conn, experiment_id)

    _migrations.run_migrations(conn)

    users = conn.execute("SELECT id, username, scopes FROM User").fetchall()
    assert len(users) == 1
    (bootstrap_id, _username, scopes) = users[0]
    assert json.loads(scopes) == ["*"], "the sole bootstrap user must be granted the wildcard scope"

    for table, row_id in (("Project", project_id), ("Experiment", experiment_id)):
        created_by, created_at = conn.execute(
            f"SELECT created_by, created_at FROM {table} WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert created_by == bootstrap_id
        assert created_at != _migrations._CREATED_AT_BRIDGE_PLACEHOLDER


def test_migration_does_not_backfill_when_nothing_predates_it(tmp_path: Path) -> None:
    """A database with no legacy rows must not manufacture a bootstrap user out of nowhere."""
    conn = _v0_connection(tmp_path / "empty.sqlite")

    _migrations.run_migrations(conn)

    assert conn.execute("SELECT count(*) FROM User").fetchone() == (0,)


def test_migration_adds_soft_delete_columns_without_backfill(tmp_path: Path) -> None:
    """`deleted_at`/`deleted_by` need no backfill: NULL is already correct for every old row."""
    conn = _v0_connection(tmp_path / "soft_delete.sqlite")
    project_id = _insert_project(conn)

    _migrations.run_migrations(conn)

    for table in ("Project", "Experiment", "Run", "Artifact"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert {"deleted_at", "deleted_by"} <= columns

    deleted_at, deleted_by = conn.execute(
        "SELECT deleted_at, deleted_by FROM Project WHERE id = ?", (project_id,)
    ).fetchone()
    assert (deleted_at, deleted_by) == (None, None)
    # the one bootstrap admin here comes from migration 2 backfilling created_by/created_at for
    # the pre-existing project row -- migration 3 itself never needs to create a user, since NULL
    # is already the correct value for a nullable column with nothing to backfill.
    assert conn.execute("SELECT count(*) FROM User").fetchone() == (1,)


def test_migration_is_a_noop_when_rerun(tmp_path: Path) -> None:
    conn = _v0_connection(tmp_path / "rerun.sqlite")
    project_id = _insert_project(conn)
    _insert_experiment(conn, project_id)

    _migrations.run_migrations(conn)
    _migrations.run_migrations(conn)  # must not raise, must not touch already-migrated tables

    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == max(v for v, _ in _migrations.MIGRATIONS)
    assert conn.execute("SELECT count(*) FROM Experiment").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM User").fetchone() == (1,), "rerun must not add a second admin"


def test_migration_hard_fails_on_preexisting_orphaned_rows(tmp_path: Path) -> None:
    """An experiment already pointing at a nonexistent project must block the migration, not hide it."""
    conn = _v0_connection(tmp_path / "orphan.sqlite")
    _insert_experiment(conn, project_id=999_999)

    with pytest.raises(RuntimeError, match="violate"):
        _migrations.run_migrations(conn)

    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == 0, "a failed migration must not be recorded as applied"


def test_run_migrations_propagates_unexpected_errors_and_does_not_advance_version(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "broken.sqlite")

    def _always_broken(_: sqlite3.Connection) -> None:
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        _migrations.run_migrations(conn, migrations=[(1, _always_broken)])

    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == 0


def test_run_migrations_treats_duplicate_column_as_already_applied(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "dup_column.sqlite")
    conn.execute("CREATE TABLE Thing (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("ALTER TABLE Thing ADD COLUMN extra TEXT")
    conn.commit()

    def _add_extra_column_again(c: sqlite3.Connection) -> None:
        c.execute("ALTER TABLE Thing ADD COLUMN extra TEXT")

    _migrations.run_migrations(conn, migrations=[(1, _add_extra_column_again)])

    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == 1
