# pyright: reportPrivateUsage=false
"""Tests for `SQLLiteStore`'s connection setup: WAL mode, busy-timeout retry, and schema-prep locking."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pendulum
import pytest
import sqlalchemy as sa

from dltrack import models
from dltrack.plugins.data_stores.sqlite import DEFAULT_BUSY_TIMEOUT_MS, SQLLiteStore
from dltrack.serve import SCHEMA_PREP_OPTION

if TYPE_CHECKING:
    from pathlib import Path


def test_connect_enables_wal_journal_mode(store: SQLLiteStore) -> None:
    ((mode,),) = store._execute(sa.text("PRAGMA journal_mode"))
    assert mode.lower() == "wal"


def test_get_or_create_defaults_busy_timeout(tmp_path: Path) -> None:
    created = SQLLiteStore.get_or_create(tmp_path / "default.sqlite")
    assert created._busy_timeout_ms == DEFAULT_BUSY_TIMEOUT_MS


def test_get_or_create_honors_a_custom_busy_timeout(tmp_path: Path) -> None:
    created = SQLLiteStore.get_or_create(tmp_path / "custom.sqlite", busy_timeout_ms=500)
    assert created._busy_timeout_ms == 500


def test_concurrent_first_time_schema_creation_does_not_race(tmp_path: Path) -> None:
    """
    Regression test for the race this module's `_begin` closes.

    Several processes starting against the same brand-new database at once can each see "no
    schema yet" and race to create one. Constructing several stores against the same path at once
    must not raise, and every one of them must end up with a working, migrated schema.
    """
    location = tmp_path / "concurrent.sqlite"
    with ThreadPoolExecutor(max_workers=5) as pool:
        stores = [
            future.result(timeout=10) for future in [pool.submit(SQLLiteStore, location) for _ in range(5)]
        ]

    for created in stores:
        assert [tuple(row) for row in created._execute(sa.select(1))] == [(1,)]
        created.dispose()


def test_writer_holding_the_database_does_not_lock_out_a_concurrent_reader(store: SQLLiteStore) -> None:
    """
    WAL mode's whole point: a reader must not block on -- or be blocked by -- an in-progress writer.

    Without WAL (the sqlite3 default), the writer thread's open transaction below would make the
    main thread's read either block for the full busy-timeout or raise `OperationalError: database
    is locked`, depending on timing. Regression coverage for that: this must return promptly instead.
    """
    writer_holds_lock = threading.Event()
    release_writer = threading.Event()

    def hold_write_lock() -> None:
        with sqlite3.connect(store._location, timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000) as conn:
            conn.execute("BEGIN IMMEDIATE")
            writer_holds_lock.set()
            release_writer.wait(timeout=5)

    writer = threading.Thread(target=hold_write_lock)
    writer.start()
    try:
        assert writer_holds_lock.wait(timeout=5)
        assert [tuple(row) for row in store._execute(sa.select(1))] == [(1,)]
    finally:
        release_writer.set()
        writer.join(timeout=5)


def test_foreign_keys_are_off_only_during_schema_prep(store: SQLLiteStore) -> None:
    """
    `_begin` turns enforcement off for the one schema-prep transaction, not for ordinary ones.

    A batch migration's table-recreate needs it off (see `_begin`'s docstring) -- but every ordinary
    connection must still enforce it, or a bug elsewhere could silently orphan a row.
    """
    ((ordinary,),) = store._execute(sa.text("PRAGMA foreign_keys"))
    assert ordinary == 1

    with store._engine.connect() as conn:
        conn = conn.execution_options(**{SCHEMA_PREP_OPTION: True})
        with conn.begin():
            ((during_schema_prep,),) = conn.execute(sa.text("PRAGMA foreign_keys")).fetchall()
    assert during_schema_prep == 0


def test_verify_schema_raises_on_a_dangling_foreign_key(store: SQLLiteStore) -> None:
    """
    `_verify_schema` is the safety net `_begin` turning enforcement off during migrations needs.

    Simulates what a buggy batch table-recreate could otherwise leave behind: an `Experiment` row
    whose `project_id` points nowhere. Enforcement has to be off to even construct that row.
    """
    with store._engine.connect() as conn:
        conn = conn.execution_options(**{SCHEMA_PREP_OPTION: True})
        with conn.begin():
            conn.execute(
                sa.insert(store.tables[models.Experiment]).values(
                    id=1,
                    project_id=999,
                    name="",
                    description="",
                    revision=0,
                    notes_revision=0,
                    created_at=pendulum.now(pendulum.UTC),
                )
            )
            with pytest.raises(RuntimeError, match="dangling foreign-key"):
                store._verify_schema(conn)
