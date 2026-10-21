"""
What every OpenTelemetry signal of dlboard shares, on either side of the wire.

One lazily built object per process (`PerProcess`), and the typed labels a measurement carries (`Attributes`).

Lives at the top level, next to `dlboard._batching`, because the client's shipping processes need it too and
`dlboard.client` can't import `dlboard.serve`. Imports nothing from `opentelemetry` at runtime, and nothing here
runs unless a signal is switched on, so a client or a local server without the `otel` extra never loads the SDK.

`PerProcess` is the part that makes this safe under `fork` (Granian's workers on Python 3.12, DataLoader workers) and
`spawn` (the client's shipping processes, the server's blob writer, see `dlboard._mp_context`): each process builds
its own exporters on first use, with its own identity, and never inherits the parent's. A provider is deliberately
never built in a process that goes on to fork -- the SDK's exporters restart their threads in a forked child, which
would export the parent's data a second time under the parent's identity.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field, fields
from multiprocessing import util
from typing import TYPE_CHECKING, Any, Final, Generic, Protocol, TypeVar

from structlog.stdlib import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from opentelemetry.util.types import AttributeValue

    from dlboard._compat import StrEnum

_log = get_logger(__name__)

_monotonic = time.monotonic
"""The clock a failed build's back-off is measured against; a test winds its own on by hand."""


class _Closeable(Protocol):
    def close(self) -> None: ...


T = TypeVar("T", bound=_Closeable)
"""Pre-3.12 `TypeVar` style (the client's floor is 3.10): what a `PerProcess` builds."""

_RETRY_FIRST_S: Final = 5.0
_RETRY_LAST_S: Final = 300.0


class PerProcess(Generic[T]):
    """
    One lazily built `T` per process: `build` runs on the first `get()` after startup *or after a fork*.

    `build` returns `None` when its signal is switched off. A call that arrives on another thread
    mid-build also gets `None`, rather than waiting -- so does one that re-enters from the build
    itself (the warning it logs on failure goes through the log processor, which asks for `LOGS`).
    A build that fails is tried again after a back-off that doubles from 5 seconds to 5
    minutes, so a setup that was broken when the process started (a bad endpoint, a full disk)
    recovers on its own instead of costing the process its telemetry for good.
    """

    def __init__(self, build: Callable[[], T | None]) -> None:
        self._build = build
        self._reset()
        if sys.platform != "win32":
            # A lock another thread held at fork time would otherwise stay locked forever in the child.
            os.register_at_fork(after_in_child=self._reset)

    def _reset(self) -> None:
        self._lock = threading.Lock()
        self._pid: int | None = None
        self._value: T | None = None
        self._retry_at = 0.0
        self._backoff_s = _RETRY_FIRST_S

    def get(self) -> T | None:
        """The `T` for the current process, built on first use; `None` when switched off or not (yet) built."""
        if self._pid == os.getpid():
            return self._value
        if _monotonic() < self._retry_at:
            return None
        with self._lock:
            if self._pid == os.getpid():
                return self._value
            if _monotonic() < self._retry_at:  # another thread has just failed
                return None
            self._pid = os.getpid()
            try:
                self._value = self._build()
            except Exception:  # noqa: BLE001 -- a down collector or bad endpoint must not take a request or startup with it
                self._pid = None  # not built: ask again once the back-off is over
                self._retry_at = _monotonic() + self._backoff_s
                _log.warning(
                    "telemetry is not available in this process; trying again in %.0f s",
                    self._backoff_s,
                    exc_info=True,
                )
                self._backoff_s = min(self._backoff_s * 2, _RETRY_LAST_S)
                return None
            if self._value is not None:
                if self._backoff_s != _RETRY_FIRST_S:
                    _log.info("telemetry is available again in this process")
                    self._backoff_s = _RETRY_FIRST_S
                # Not `atexit`: a Granian worker exits through multiprocessing's `_exit_function`, which
                # runs these. 50 is below the blob store's 100, so its writer drains (and records its
                # last metrics) first.
                util.Finalize(None, self.close, exitpriority=50)
            return self._value

    def peek(self) -> T | None:
        """The `T` this process has already built, if any. Never builds one: for what only matters once something has."""
        return self._value if self._pid == os.getpid() else None

    def close(self) -> None:
        """Flush and shut down what this process built (a no-op in any other process), then forget it."""
        value = self._value if self._pid == os.getpid() else None
        self._reset()
        if value is not None:
            try:
                value.close()
            except Exception:  # noqa: BLE001 -- flushing on the way out must not turn a clean exit into a crash
                _log.warning("could not flush telemetry on exit", exc_info=True)


_KEY: Final = "attribute_key"


def attribute(key: StrEnum) -> Any:  # noqa: ANN401 -- a dataclass `field()`, typed by the annotation of the field it sets
    """A field of an `Attributes` class that is reported under `key`: one of `AttributeKey`, or a `StrEnum` of your own."""
    return field(metadata={_KEY: key})


@dataclass(frozen=True, slots=True)
class Attributes:
    """
    The labels on one measurement. Subclass it (as a frozen dataclass) with a field per label, each set by `attribute`.

    A call site builds one, so a wrong label or a wrong type is caught by the type checker, and only
    an instrument that is actually recording turns it into the mapping OpenTelemetry wants.
    """

    def as_otel(self) -> dict[str, AttributeValue]:
        """The labels, under the names `attribute` gave them."""
        return {
            str(f.metadata[_KEY]): getattr(self, f.name)  # dlboard-ignore[forbid-string-ignore]
            for f in fields(self)
        }
