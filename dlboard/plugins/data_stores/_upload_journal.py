"""
The uploads a server has answered for but not finished: each one's bytes, and what they are, kept on disk until its ref is recorded.

An upload is answered once its entry is on disk, and the client may then discard its own copy: from there the entry is the
only copy, so it goes only once its ref is recorded, or the store refuses it for good. Each entry is locked (`flock`) by the
process holding it, from the moment its name appears, for as long as it holds it. A lock dies with its process, so an
entry nobody holds is one whose process died, and any process sharing the directory takes it over (`adopt`).

An entry outlives any process, but is not fsynced: the kernel writes it to disk within its writeback delay (about 30
seconds by default), so an OS crash or power loss can lose that much. Keeping a host up through a power cut is the
hardware's job; fsyncing every upload cost ten times what staging one does.
"""

from __future__ import annotations

import fcntl
import os
import time
from dataclasses import dataclass
from threading import Condition
from typing import IO, TYPE_CHECKING, Final
from uuid import UUID, uuid4

from pydantic import AnyUrl, BaseModel, ConfigDict, ValidationError
from structlog.stdlib import get_logger

from dlboard import models

if TYPE_CHECKING:
    from pathlib import Path

    from werkzeug.datastructures import FileStorage

_log = get_logger(__name__)

_ENTRY: Final = ".json"
_PART: Final = ".part"
"""An entry still being written: renamed to `_ENTRY` once whole, so a reader never sees half of one."""

_STALE_SEC: Final = 24 * 60 * 60
"""Staged bytes with no entry this old belong to a request that died before it answered: nobody was told they were kept."""

_COPY_BUFFER: Final = int(1e6)


class _Entry(BaseModel):
    """What an entry says of its upload, beside its bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact: models.NewArtifact
    ref: AnyUrl


@dataclass(frozen=True, slots=True)
class Held:
    """An upload this process holds: answered for, not yet finished."""

    artifact: models.NewArtifact
    ref: AnyUrl
    staged: Path
    recovered: bool
    """Taken over from a process that died, which may have recorded its ref before it could finish it."""
    lock: IO[bytes]

    @property
    def name(self) -> str:
        return self.staged.name


def _is_a_name(name: str) -> bool:
    """Whether `name` is one `accept` gives an upload: a UUID."""
    try:
        UUID(name)
    except ValueError:
        return False
    return True


class UploadJournal:
    """The entries in one staging directory, and which of them this process holds."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        self._held: dict[str, Held] = {}
        self._changed = Condition()

    def accept(self, artifact: models.NewArtifact, ref: AnyUrl, file: FileStorage) -> Held:
        """Keep `file` as the upload of `artifact` to `ref`, held by this process."""
        staged = self._dir / str(uuid4())
        part = staged.with_suffix(_PART)
        lock: IO[bytes] | None = None
        try:
            with staged.open("xb") as out:
                file.save(out, buffer_size=_COPY_BUFFER)
            lock = part.open("xb")
            fcntl.flock(lock, fcntl.LOCK_EX)  # a name nobody else knows yet: never waits
            lock.write(_Entry(artifact=artifact, ref=ref).model_dump_json().encode())
            lock.flush()  # the file stays open, as the lock: written out before another process can read it
            part.replace(staged.with_suffix(_ENTRY))  # the lock is the file's, and goes with it
        except BaseException:
            if lock is not None:
                lock.close()
            part.unlink(missing_ok=True)
            staged.unlink(missing_ok=True)
            raise
        return self._hold(Held(artifact, ref, staged, recovered=False, lock=lock))

    def adopt(self) -> list[Held]:
        """Take over every entry no live process holds: each one's process died before it could finish it."""
        adopted: list[Held] = []
        for path in self._dir.glob(f"*{_ENTRY}"):
            if not _is_a_name(path.stem) or path.stem in self._held:
                continue
            try:
                lock = path.open("rb")
            except FileNotFoundError:
                continue  # finished since the listing
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Finished between the open and the lock: the path no longer names the file the lock is on.
                if path.stat().st_ino != os.fstat(lock.fileno()).st_ino:
                    lock.close()
                    continue
                entry = _Entry.model_validate_json(lock.read())
            except (BlockingIOError, FileNotFoundError):
                lock.close()  # held by a live process, or finished since the open
                continue
            except ValidationError:
                lock.close()
                _log.exception("An upload's journal entry cannot be read; leaving it", path=str(path))
                continue
            adopted.append(
                self._hold(Held(entry.artifact, entry.ref, self._dir / path.stem, recovered=True, lock=lock))
            )
        return adopted

    def _hold(self, held: Held) -> Held:
        with self._changed:
            self._held[held.name] = held
        return held

    def holds(self, name: str) -> bool:
        """Whether this process still holds the upload `name`: false once it is finished, or let go."""
        return name in self._held

    def held(self) -> list[Held]:
        """Every upload this process holds."""
        with self._changed:
            return list(self._held.values())

    def wait_for_room(self, limit: float, timeout: float) -> bool:
        """Wait up to `timeout` for this process to hold fewer than `limit` uploads; whether it does."""
        with self._changed:
            return self._changed.wait_for(lambda: len(self._held) < limit, timeout)

    def finish(self, name: str) -> bool:
        """Delete the upload `name`, its ref recorded or refused for good; whether this process held it."""
        with self._changed:
            held = self._held.pop(name, None)
            self._changed.notify_all()
        if held is None:
            return False
        # Bytes first: an entry left without them is one whose blob was written, which `adopt` copes with.
        held.staged.unlink(missing_ok=True)
        held.staged.with_suffix(_ENTRY).unlink(missing_ok=True)
        held.lock.close()
        return True

    def let_go(self) -> None:
        """Stop holding every upload, leaving each for a process that will finish it: this one is going away."""
        with self._changed:
            held = self._held
            self._held = {}
            self._changed.notify_all()
        for upload in held.values():
            upload.lock.close()

    def sweep(self) -> None:
        """
        Delete what a request left behind when it died before answering: bytes with no entry, or half an entry.

        The directory may be the shared temp directory, so only a name the journal would have given is its to delete, and
        one that cannot be removed is left with a warning rather than failing the store's start.
        """
        cutoff = time.time() - _STALE_SEC
        for leftover in self._dir.iterdir():
            orphaned = leftover.suffix == "" and not leftover.with_suffix(_ENTRY).exists()
            if not _is_a_name(leftover.stem) or not (orphaned or leftover.suffix == _PART):
                continue
            try:
                if leftover.is_file() and leftover.stat().st_mtime < cutoff:
                    leftover.unlink(missing_ok=True)
            except OSError:
                _log.warning("Could not delete a stale staged upload", path=str(leftover), exc_info=True)
