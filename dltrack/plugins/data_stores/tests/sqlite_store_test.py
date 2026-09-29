# pyright: reportPrivateUsage=false
"""Tests for `SQLLiteStore`'s connection setup: WAL mode and busy-timeout retry under contention."""

from __future__ import annotations

import sqlite3
import threading
from typing import TYPE_CHECKING

import sqlalchemy as sa

from dltrack.plugins.data_stores.sqlite import DEFAULT_BUSY_TIMEOUT_MS, SQLLiteStore

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
