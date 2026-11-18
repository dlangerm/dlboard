"""
A run's console output -- what the script writes to `sys.stdout` and `sys.stderr` -- captured into the run, in chunks.

Deliberately small (#697): this shows the end of a run's output next to its metrics, and is not a log service. Python's
two streams are wrapped, and a write goes to the real stream, then into a bounded buffer: no queue per line, no syscall,
no parsing. A timer thread cuts the buffer into a chunk every `interval_s` (sooner once it holds `chunk_kb`), with
carriage-return updates (a progress bar) collapsed to their last state, and logs it as an ordinary artifact
(`models.CONSOLE_KEY`, at the chunk's sequence number as its step): the server stores it as any other, and the console
chart reads it back a chunk at a time. A write that finds the buffer full is dropped and counted, never waited on, and
past `max_mb` a run's capture stops. Native (C/C++) and child-process output, which never passes through Python's
streams, is not captured: that would need the file descriptors redirected through a pipe, and a reader that stalls
there would block every `print` of the training loop.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final, TextIO

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard.models import CONSOLE_KEY, NewArtifact, TagValue

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

_KB: Final = 1024
_BUFFERED_CHUNKS: Final = 4
"""How many chunks' worth a buffer holds before a write is dropped: room for the timer thread to fall behind a burst."""
DROPPED_TAG: Final = "dropped_chars"
"""A chunk's tag: how much was written but dropped, as the buffer was full, since the chunk before it."""
CAPPED_TAG: Final = "capped"
"""A chunk's tag: the run reached `max_mb`, and nothing after this chunk was captured."""


class ConsoleSettings(BaseSettings):
    """Whether, and how, a run captures its console output (`DLBOARD_CONSOLE_*`)."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_CONSOLE_", frozen=True)

    enabled: bool = True
    """Capture what the script writes to `sys.stdout` and `sys.stderr`, from the run's start until it is finished."""
    interval_s: float = Field(default=30.0, ge=1.0)
    """Seconds between chunks: also how much of the end of a run's output a `kill -9` can cost."""
    chunk_kb: int = Field(default=256, ge=1)
    """A chunk is cut sooner once this much is waiting, so a chunk the console chart fetches stays small."""
    max_mb: int = Field(default=10, ge=0)
    """The most a run captures; past it, capture stops and the last chunk says so."""


class ConsoleChunk(BaseModel, frozen=True, extra="forbid"):
    """A stretch of a run's console output, as the artifact it is logged as: a text file, at its sequence number."""

    data: bytes
    step: int
    tags: Mapping[str, TagValue] = Field(default_factory=dict[str, TagValue])
    key: str = CONSOLE_KEY

    def to_artifact(self, local_temp: Path, run_id: int, experiment_id: int) -> tuple[NewArtifact, Path]:
        """Write the chunk under `local_temp`, to upload."""
        path = local_temp / f"console-{self.step:06d}.txt"
        path.write_bytes(self.data)
        new = NewArtifact(
            key=self.key,
            fname=path.name,
            run_id=run_id,
            experiment_id=experiment_id,
            step=self.step,
            tags=dict(self.tags),
        )
        return new, path


def collapse_carriage_returns(text: str) -> str:
    r"""Each line as it last looked: a progress bar redrawn with `\r` a thousand times is its final state, once."""
    return "\n".join(
        next((part for part in reversed(line.split("\r")) if part), "") for line in text.split("\n")
    )


class _Tee:
    """A stream that writes to the one it wraps, then hands what it wrote to the capture. Everything else is the stream's."""

    def __init__(self, stream: TextIO, capture: ConsoleCapture) -> None:
        self._stream, self._capture = stream, capture

    def write(self, text: str) -> int:
        written = self._stream.write(text)
        self._capture.append(text)
        return written

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def __getattr__(
        self, name: str
    ) -> object:  # dlboard-ignore[forbid-string-ignore] -- whatever the stream has
        """Everything a stream has but `write`/`writelines` (`flush`, `isatty`, `encoding`, ...) is the stream's own."""
        return getattr(self._stream, name)  # dlboard-ignore[forbid-string-ignore] -- the delegation itself


class ConsoleCapture:
    """
    The capture of one run's console: installed over `sys.stdout`/`sys.stderr`, cutting a chunk into `ship` on its timer.

    Thread-safe: any thread may write while the timer thread cuts. `stop` cuts the last chunk, so a crash's traceback
    (written to `sys.stderr` before the interpreter's exit hooks run) is in it.
    """

    def __init__(self, settings: ConsoleSettings, ship: Callable[[ConsoleChunk], None]) -> None:
        self._settings, self._ship = settings, ship
        self._lock = threading.Lock()
        self._parts: list[str] = []
        self._held = self._dropped = self._shipped = self._sequence = 0
        self._capped = self._stopping = False
        self._cut = threading.Event()
        self._streams: tuple[TextIO, TextIO] = (sys.stdout, sys.stderr)
        self._tees = (_Tee(self._streams[0], self), _Tee(self._streams[1], self))
        self._thread = threading.Thread(target=self._cut_until_stopped, name="dlboard-console", daemon=True)

    @classmethod
    def start(cls, settings: ConsoleSettings, ship: Callable[[ConsoleChunk], None]) -> ConsoleCapture | None:
        """Wrap the streams and start cutting chunks, or `None` when turned off."""
        if not settings.enabled or settings.max_mb == 0:
            return None
        capture = cls(settings, ship)
        sys.stdout, sys.stderr = capture._tees
        capture._thread.start()
        return capture

    def append(self, text: str) -> None:
        """Hold what was written for the next chunk; drop it, counted, if the buffer is full. Never blocks on a cut."""
        with self._lock:
            if self._capped or self._stopping:
                return
            if self._held + len(text) > self._settings.chunk_kb * _KB * _BUFFERED_CHUNKS:
                self._dropped += len(text)
                return
            self._parts.append(text)
            self._held += len(text)
            full = self._held >= self._settings.chunk_kb * _KB
        if full:
            self._cut.set()

    def cut(self) -> None:
        """Log what is held as the next chunk, if anything is (or was dropped)."""
        with self._lock:
            text, dropped = "".join(self._parts), self._dropped
            self._parts.clear()
            self._held = self._dropped = 0
            if self._capped or not (text or dropped):
                return
            data = collapse_carriage_returns(text).encode(errors="replace")
            room = self._settings.max_mb * _KB * _KB - self._shipped
            tags: dict[str, TagValue] = {DROPPED_TAG: dropped} if dropped else {}
            if len(data) >= room:
                data, self._capped = data[:room], True
                tags[CAPPED_TAG] = True
            chunk = ConsoleChunk(data=data, step=self._sequence, tags=tags)
            self._sequence += 1
            self._shipped += len(data)
        self._ship(chunk)

    def _cut_until_stopped(self) -> None:
        while True:
            self._cut.wait(self._settings.interval_s)
            self._cut.clear()
            if self._stopping:
                return
            self.cut()

    def stop(self) -> None:
        """Put the streams back (unless something has wrapped them since), stop the timer, and cut the last chunk."""
        if sys.stdout is self._tees[0]:
            sys.stdout = self._streams[0]
        if sys.stderr is self._tees[1]:
            sys.stderr = self._streams[1]
        self.cut()
        with self._lock:
            self._stopping = True
        self._cut.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(1.0)
