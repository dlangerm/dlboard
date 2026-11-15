"""`UploadJournal`: which process holds an upload, and what its sweep leaves."""

from __future__ import annotations

import io
import os
from typing import TYPE_CHECKING

from pydantic import AnyUrl
from werkzeug.datastructures import FileStorage

from dlboard import models
from dlboard.plugins.data_stores._upload_journal import UploadJournal

if TYPE_CHECKING:
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

    assert other.adopt() == []
    holder.let_go()
    (adopted,) = other.adopt()

    assert (adopted.name, adopted.artifact, adopted.recovered) == (held.name, held.artifact, True)
    assert other.adopt() == []  # held now, by `other`
    other.let_go()


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
