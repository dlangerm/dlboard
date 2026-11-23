r"""
A run's console output -- what the script writes to `sys.stdout` and `sys.stderr` -- captured into the run, in chunks.

Deliberately small (#697): this shows the end of a run's output next to its metrics, and is not a log service. Python's
two streams are wrapped, and a write goes to the real stream, then into a bounded buffer: no queue per line, no syscall,
no parsing. A timer thread cuts the buffer into a chunk every `interval_s` (sooner once it holds `chunk_kb`), at a line
end, with what is drawn in place (a progress bar's `\r`, rich's lines drawn again) as it last looked, and logs it as an ordinary artifact
(`models.CONSOLE_KEY`, at the chunk's sequence number as its step): the server stores it as any other, and the console
chart reads it back a chunk at a time. A write that finds the buffer full is dropped and counted, never waited on, and
past `max_mb` a run's capture stops. Native (C/C++) and child-process output, which never passes through Python's
streams, is not captured: that would need the file descriptors redirected through a pipe, and a reader that stalls
there would block every `print` of the training loop.
"""

from __future__ import annotations

import logging
import re
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


_DRAWING: Final = re.compile(r"(\n|\r|\x1b\[2K|\x1b\[\d*A)")
"""What draws over what was written: a line end, a carriage return, erasing the line, the cursor going up."""
_UP: Final = re.compile(r"\x1b\[(\d*)A")


def as_last_drawn(text: str) -> str:
    r"""
    The lines as a terminal leaves them: what is drawn again in place is kept once, as it last looked.

    A progress bar drawn again with `\r` a thousand times is its last state, once, and so are rich's lines drawn again
    by going back up over them (two progress bars, training and validation). A line erased is blank, as a terminal
    shows it, even if nothing is drawn on it again (a bar taken away); text written after a line is returned to replaces
    it, and one that is only returned to (`\r\n`) keeps what it had.
    """
    lines, at, replacing = [""], 0, False
    for part in _DRAWING.split(text):
        if part == "\n":
            at += 1
            if at == len(lines):
                lines.append("")
            replacing = True
        elif part == "\r":
            replacing = True
        elif part == "\x1b[2K":  # erased now, as a terminal shows it, even if nothing is drawn there again
            lines[at], replacing = "", True
        elif up := _UP.fullmatch(part):
            at, replacing = max(0, at - int(up[1] or 1)), True
        elif part:
            lines[at], replacing = (part if replacing else lines[at] + part), False
    return "\n".join(lines)


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
        handlers: list[logging.StreamHandler[TextIO | _Tee]] = [
            handler
            for logger in (logging.root, *logging.root.manager.loggerDict.values())
            if isinstance(logger, logging.Logger)
            for handler in logger.handlers
            if isinstance(handler, logging.StreamHandler)
        ]
        self._handlers = [
            (handler, at)
            for handler in handlers
            for at, stream in enumerate(self._streams)
            if handler.stream is stream
        ]
        """
        Every handler writing to one of the streams, and which, of every logger made so far: `start` points them at the
        tees. One that looks the stream up each time it writes (logging's last resort, pytest's) follows the tee itself.
        """
        self._thread = threading.Thread(target=self._cut_until_stopped, name="dlboard-console", daemon=True)

    @classmethod
    def start(cls, settings: ConsoleSettings, ship: Callable[[ConsoleChunk], None]) -> ConsoleCapture | None:
        """
        Wrap the streams and start cutting chunks, or `None` when turned off.

        A logging handler holds the stream it was made with, so one made before the run (Lightning's, made as it is
        imported; `logging.basicConfig`'s) is pointed at the wrapped stream too, or what it logs would never be captured.
        """
        if not settings.enabled or settings.max_mb == 0:
            return None
        capture = cls(settings, ship)
        sys.stdout, sys.stderr = capture._tees
        for handler, at in capture._handlers:
            if handler.stream is capture._streams[at]:  # not one that follows `sys.stderr` to the tee already
                handler.setStream(capture._tees[at])
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
        """
        Log what is held as the next chunk, if anything is (or was dropped), up to its last line end.

        The rest of a line goes in the next chunk, so a line is never split between two: `print` writes a line, then its
        end. All of it goes when there is no line end (a progress bar drawn in place), and when the capture is stopping.
        """
        with self._lock:
            held, dropped = "".join(self._parts), self._dropped
            end = len(held) if self._stopping else held.rfind("\n") + 1 or len(held)
            text, rest = held[:end], held[end:]
            self._parts[:] = [rest] if rest else []
            self._held, self._dropped = len(rest), 0
            if self._capped or not (text or dropped):
                return
            data = as_last_drawn(text).encode(errors="replace")
            room = self._settings.max_mb * _KB * _KB - self._shipped
            tags: dict[str, TagValue] = {DROPPED_TAG: dropped} if dropped else {}
            if len(data) >= room:  # cut on a character: what was cut from the last one is dropped with it
                data, self._capped = data[:room].decode(errors="ignore").encode(), True
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
        for handler, at in self._handlers:
            if handler.stream is self._tees[at]:
                handler.setStream(self._streams[at])
        with self._lock:
            self._stopping = True
        self.cut()
        self._cut.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(1.0)
