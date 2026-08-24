# pyright: reportPrivateUsage=false
"""Tests for the schema migration runner in `_migrations.py`.

Builds a version-0 schema by hand (today's pre-foreign-key table shapes, via `create_table_sql`
without a foreign-key map -- exactly what an existing `~/.dltrack.sqlite` looks like before this
change) and runs the real migration chain against it, so a broken migration fails the test suite
loudly rather than only failing silently against someone's production database.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from dltrack import models
from dltrack.serve import sql
from dltrack.serve._backend import _migrations

if TYPE_CHECKING:
    from pathlib import Path


def _v0_connection(db_path: Path) -> sqlite3.Connection:
    """
    A connection to a database with the pre-migration schema: no foreign keys declared.

    Mirrors exactly what `SQLStoreBase.__init__` used to create for every table before this
    change -- all seven tables, none of them with a `FOREIGN KEY` clause -- since that's the
    real shape any existing `~/.dltrack.sqlite` has on disk.
    """
    conn = sqlite3.connect(db_path)
    for table in (
        models.Project,
        models.Experiment,
        models.Run,
        models.UnderlyingMetricTableEntry,
        models.HyperParams,
        models.Artifact,
        models.Page,
    ):
        conn.execute(sql.create_table_sql(table))
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
    assert experiment_fks == {("project_id", "Project")}

    run_fks = {(row[3], row[2]) for row in conn.execute("PRAGMA foreign_key_list(Run)")}
    assert run_fks == {("experiment_id", "Experiment")}

    metric_fks = {
        (row[3], row[2]) for row in conn.execute("PRAGMA foreign_key_list(UnderlyingMetricTableEntry)")
    }
    assert metric_fks == {("experiment_id", "Experiment"), ("run_id", "Run")}

    assert conn.execute("SELECT name FROM Project WHERE id = ?", (project_id,)).fetchone() == ("keep-me",)
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == 1


def test_migration_is_a_noop_when_rerun(tmp_path: Path) -> None:
    conn = _v0_connection(tmp_path / "rerun.sqlite")
    project_id = _insert_project(conn)
    _insert_experiment(conn, project_id)

    _migrations.run_migrations(conn)
    _migrations.run_migrations(conn)  # must not raise, must not touch already-migrated tables

    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == 1
    assert conn.execute("SELECT count(*) FROM Experiment").fetchone() == (1,)


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
