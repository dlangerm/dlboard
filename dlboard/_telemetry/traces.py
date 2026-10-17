"""
Distributed traces: how a request travels from a training script, through its shipping process, into the server.

Off unless asked for, and never in the way of what it traces.

Everything an instrumented module touches lives here and imports nothing from `opentelemetry` at runtime: `span` is
a context manager that is one pid comparison and a shared no-op while tracing is off, so a client or a local server
without the `otel` extra never loads the SDK. The SDK half (`dlboard._telemetry._sdk`) is imported by the first call
that finds tracing switched on, and only uses OpenTelemetry API that exists in the oldest release the client's
`otel` extra allows, so enabling traces never forces a user to upgrade or downgrade their own OpenTelemetry.

Each process builds its own tracer on first use (see `PerProcess`), so `fork` and `spawn` behave alike. A trace
crosses a process boundary that no library instruments -- an HTTP request is carried by W3C `traceparent` headers,
and a `multiprocessing` queue by a `Carrier` put in the item itself -- through `carrier()` and `span(parent=...)`.

Telemetry never takes down what it traces: whatever goes wrong inside a tracing call is logged, once, and swallowed,
and the traced code runs exactly as it would have.
"""

from __future__ import annotations

import importlib.util
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final, TypeAlias, TypeVar

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from structlog.stdlib import get_logger

from dlboard._compat import StrEnum
from dlboard._telemetry import PerProcess

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

    from dlboard._telemetry import Attributes
    from dlboard._telemetry._sdk import Opened, Traces

_log = get_logger(__name__)

R = TypeVar("R")
"""Pre-3.12 `TypeVar` style (the client's floor is 3.10): what a tracing call returns."""

Carrier: TypeAlias = dict[str, str]
"""
A trace context as W3C `traceparent` headers: what to hand to whatever carries a request onward.

Plain strings, so it can ride in an HTTP header, a `multiprocessing` queue, or a log line. Empty when tracing is off.
"""


class SpanKind(StrEnum):
    """What a span is to the trace around it (OpenTelemetry's span kinds)."""

    INTERNAL = "internal"
    CLIENT = "client"
    SERVER = "server"
    PRODUCER = "producer"
    CONSUMER = "consumer"


class TraceKey(StrEnum):
    """The names a span's attributes are reported under: OpenTelemetry's semantic conventions where one exists."""

    RUN_ID = "dlboard.run.id"
    EXPERIMENT_ID = "dlboard.experiment.id"


class TraceSettings(BaseSettings):
    """`DLBOARD_TELEMETRY_TRACES`, read the same way by the client and the server. Off, nothing here runs."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_TELEMETRY_")

    traces: bool = False
    """Send distributed traces over OTLP. The exporter reads the standard `OTEL_EXPORTER_OTLP_*` variables."""

    @model_validator(mode="after")
    def _require_sdk(self) -> TraceSettings:
        if self.traces and not sdk_installed():
            msg = (
                "DLBOARD_TELEMETRY_TRACES is set, but the OpenTelemetry SDK is missing: "
                "pip install 'dlboard-client[otel]' (or 'dlboard[otel]' on the server)"
            )
            raise ValueError(msg)
        return self


def sdk_installed() -> bool:
    """Whether the OpenTelemetry SDK can be imported, without importing it."""
    try:
        return importlib.util.find_spec("opentelemetry.sdk") is not None
    except (ImportError, ValueError):  # `opentelemetry` itself absent (or blocked)
        return False


_DEFAULT_SERVICE: Final = "dlboard"
_service = _DEFAULT_SERVICE


def name_service(name: str) -> None:
    """
    Say what this process is called in a trace (`service.name`), before its first span.

    The server is `dlboard`; a training process and its shipping processes call themselves `dlboard-client`.
    `OTEL_SERVICE_NAME` overrides either. A process whose tracer is already built keeps the name it has.
    """
    global _service
    _service = name


def _build_traces() -> Traces | None:
    if not TraceSettings().traces:
        return None
    # Deferred: the SDK is only imported once tracing is on.
    from dlboard._telemetry._sdk import build_traces

    return build_traces(_service)


TRACES: Final = PerProcess(_build_traces)
"""This process's tracer, built on first use (`None` while tracing is off)."""

_warned = threading.Event()


def _swallowed() -> None:
    """Log a tracing failure the first time one happens; later ones are as silent as the traced code is unaffected."""
    if not _warned.is_set():
        _warned.set()
        _log.warning("tracing failed; further failures of it are not logged", exc_info=True)


class ActiveSpan:
    """A span that is open: what a traced block can say about it. A no-op while tracing is off."""

    __slots__ = ("_opened",)

    def __init__(self, opened: Opened | None) -> None:
        self._opened = opened

    def set(self, attributes: Attributes) -> None:
        """Add `attributes` to the span."""
        self._call(lambda opened: opened.set(attributes.as_otel()))

    def event(self, name: str, attributes: Attributes | None = None) -> None:
        """Note something that happened at this moment (a retry, a split) without opening a span for it."""
        self._call(lambda opened: opened.event(name, None if attributes is None else attributes.as_otel()))

    def failed(self, exc: BaseException) -> None:
        """Mark the span as failed with `exc` -- for an error the traced code handles, so it never reaches the block's end."""
        self._call(lambda opened: opened.fail(exc))

    def _call(self, call: Callable[[Opened], None]) -> None:
        if self._opened is not None:
            try:
                call(self._opened)
            except Exception:  # noqa: BLE001 -- see the module docstring
                _swallowed()


_OFF: Final = ActiveSpan(None)


@contextmanager
def span(
    name: str,
    kind: SpanKind,
    attributes: Attributes | None = None,
    *,
    parent: Carrier | None = None,
    links: Sequence[Carrier] = (),
) -> Generator[ActiveSpan]:
    """
    Run a block inside a span called `name`, a child of whatever span is open here (or of `parent`, if given).

    An exception that leaves the block is recorded on the span (its type, message and traceback, never local
    variables) and propagates unchanged. `links` relate the span to others, for work that serves many requests at once.
    """
    opened = _start(name, kind, attributes, parent, links)
    if opened is None:
        yield _OFF
        return
    active = ActiveSpan(opened)
    try:
        yield active
    except Exception as exc:
        active.failed(exc)
        raise
    finally:
        try:
            opened.end()
        except Exception:  # noqa: BLE001 -- see the module docstring
            _swallowed()


def _start(
    name: str,
    kind: SpanKind,
    attributes: Attributes | None,
    parent: Carrier | None,
    links: Sequence[Carrier],
) -> Opened | None:
    try:
        traces = TRACES.get()
        if traces is None:
            return None
        return traces.start(name, kind, None if attributes is None else attributes.as_otel(), parent, links)
    except Exception:  # noqa: BLE001 -- see the module docstring
        _swallowed()
        return None


def _traced(call: Callable[[Traces], R], default: R) -> R:
    try:
        traces = TRACES.get()
        return default if traces is None else call(traces)
    except Exception:  # noqa: BLE001 -- see the module docstring
        _swallowed()
        return default


def carrier() -> Carrier:
    """The current trace context, to carry to another process or thread: empty if tracing is off or no span is open."""
    return _traced(lambda traces: traces.carrier(), {})


def trace_ids() -> tuple[str, str] | None:
    """The open span's trace id and span id (as hex), to put in a log line; `None` if there is none."""
    return _traced(lambda traces: traces.ids(), None)


def flush() -> None:
    """Export every finished span now, before the process might be killed (a daemon shipping process, a worker)."""
    _traced(lambda traces: traces.flush(), None)
