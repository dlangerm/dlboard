"""`UploadJournal`: which process holds an upload, and what its sweep leaves."""

from __future__ import annotations

import io
import math
import os
from typing import TYPE_CHECKING

import pytest
from pydantic import AnyUrl
from werkzeug.datastructures import FileStorage

from dlboard import models
from dlboard.plugins.data_stores._upload_journal import UploadJournal

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from dlboard.plugins.data_stores._upload_journal import Held


def _accept(journal: UploadJournal) -> Held:
    new_artifact = models.NewArtifact(key="img", fname="a.png", run_id=1, experiment_id=1, step=0)
    return journal.accept(
        new_artifact, AnyUrl("file:///a.png"), FileStorage(io.BytesIO(b"data"), filename="a.png")
    )


def test_an_upload_is_taken_over_only_once_the_process_holding_it_lets_go(tmp_path: Path) -> None:
    """A lock belongs to the open file, so a second journal in one process stands in for a second process."""
    holder, other = UploadJournal(tmp_path), UploadJournal(tmp_path)
    held = _accept(holder)

    assert other.adopt(math.inf) == []
    holder.let_go()
    (adopted,) = other.adopt(math.inf)

    assert (adopted.name, adopted.artifact) == (held.name, held.artifact)
    assert other.adopt(math.inf) == []  # held now, by `other`
    other.let_go()


def test_a_process_takes_over_no_more_uploads_than_its_limit(tmp_path: Path) -> None:
    """A backlog is shared out: each upload held is an open file, and one process must not open all of them."""
    holder, other = UploadJournal(tmp_path), UploadJournal(tmp_path)
    for _ in range(3):
        _accept(holder)
    holder.let_go()

    assert len(other.adopt(2)) == 2
    assert other.adopt(2) == []
    other.finish(other.held()[0].name)
    assert len(other.adopt(2)) == 1
    other.let_go()


def _emptied(_entry: bytes) -> bytes:
    return b""


def _cut_short(entry: bytes) -> bytes:
    return entry[: len(entry) // 2]


def _from_a_newer_version(entry: bytes) -> bytes:
    return entry[:-1] + b',"priority":1}'


@pytest.mark.parametrize(
    ("damage", "kept"),
    [
        pytest.param(_emptied, False, id="emptied by a crash"),
        pytest.param(_cut_short, False, id="cut short by a crash"),
        pytest.param(_from_a_newer_version, True, id="written by a newer version"),
    ],
)
def test_an_entry_that_cannot_be_read_is_deleted_only_if_a_crash_cut_it_short(
    tmp_path: Path, damage: Callable[[bytes], bytes], *, kept: bool
) -> None:
    """A newer version's entry is kept for it, after a rollback: it can still be finished once that version is back."""
    journal = UploadJournal(tmp_path)
    held = _accept(journal)
    journal.let_go()
    entry = held.staged.with_suffix(".json")
    entry.write_bytes(damage(entry.read_bytes()))
    assert UploadJournal(tmp_path).adopt(math.inf) == []

    assert (entry.exists(), held.staged.exists()) == (kept, kept)


def test_a_finished_upload_leaves_nothing_behind(tmp_path: Path) -> None:
    journal = UploadJournal(tmp_path)
    held = _accept(journal)

    assert journal.finish(held.name)

    assert list(tmp_path.iterdir()) == []
    assert not journal.finish(held.name)


def test_the_sweep_keeps_an_uploads_bytes_however_old_while_its_entry_is_there(tmp_path: Path) -> None:
    journal = UploadJournal(tmp_path)
    held = _accept(journal)
    journal.let_go()
    os.utime(held.staged, (0, 0))

    UploadJournal(tmp_path).sweep()

    assert held.staged.exists()
