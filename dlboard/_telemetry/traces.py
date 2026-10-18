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
import sys
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated, Any, Final, TypeAlias, TypeVar

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
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
    ITEMS = "dlboard.batch.items"
    SHIP_KIND = "dlboard.ship.kind"
    HTTP_METHOD = "http.request.method"
    URL_TEMPLATE = "url.template"
    SERVER_ADDRESS = "server.address"
    SERVER_PORT = "server.port"
    HTTP_STATUS_CODE = "http.response.status_code"
    EXPERIMENT_ID = "dlboard.experiment.id"
    REQUEST_ID = "dlboard.request_id"
    STARTUP_DEPENDENCY = "dlboard.startup.dependency"
    STARTUP_ATTEMPT = "dlboard.startup.attempt"


class OtlpProtocol(StrEnum):
    """The values of the standard `OTEL_EXPORTER_OTLP_PROTOCOL`."""

    GRPC = "grpc"
    HTTP_PROTOBUF = "http/protobuf"
    HTTP_JSON = "http/json"


class OtlpExportSettings(BaseSettings):
    """The standard variables that choose the OTLP wire protocol, which the exporter dlboard uses does not honour."""

    model_config = SettingsConfigDict(env_prefix="OTEL_EXPORTER_OTLP_")

    protocol: OtlpProtocol | None = None
    """For every signal, unless `traces_protocol` says otherwise."""
    traces_protocol: OtlpProtocol | None = None


class TracesExporter(StrEnum):
    """The values of the standard `OTEL_TRACES_EXPORTER` that dlboard can honour."""

    OTLP = "otlp"
    OTLP_PROTO_HTTP = "otlp_proto_http"
    OTLP_PROTO_GRPC = "otlp_proto_grpc"
    CONSOLE = "console"
    NONE = "none"


class TracesExporterSettings(BaseSettings):
    """The standard variable that says where spans go: OTLP, the console, or nowhere (spans still carry a trace)."""

    model_config = SettingsConfigDict(env_prefix="OTEL_")

    traces_exporter: Annotated[frozenset[TracesExporter], NoDecode] = frozenset({TracesExporter.OTLP})
    """Comma-separated. `none` exports nothing, though the traces still cross processes."""

    @field_validator("traces_exporter", mode="before")
    @classmethod
    def _split_commas(cls, value: Any) -> Any:  # noqa: ANN401 -- a `mode="before"` validator sees the raw input
        return [v.strip().lower() for v in value.split(",") if v.strip()] if isinstance(value, str) else value


_GRPC_UNSUPPORTED: Final = (
    "DLBOARD_TELEMETRY_TRACES exports OTLP over HTTP, not gRPC: unset OTEL_EXPORTER_OTLP_PROTOCOL (or set it to "
    "http/protobuf), use OTEL_TRACES_EXPORTER=otlp, and point OTEL_EXPORTER_OTLP_ENDPOINT at the collector's HTTP "
    "port, usually 4318"
)


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

    @model_validator(mode="after")
    def _require_a_supported_export(self) -> TraceSettings:
        """Spans sent where they cannot go would all fail; say so at startup rather than in an export error later."""
        if not self.traces:
            return self
        exporters = TracesExporterSettings().traces_exporter
        if TracesExporter.OTLP_PROTO_GRPC in exporters:
            raise ValueError(_GRPC_UNSUPPORTED)
        if exporters.isdisjoint({TracesExporter.OTLP, TracesExporter.OTLP_PROTO_HTTP}):
            return self  # the console, or nowhere: no OTLP protocol is in play
        export = OtlpExportSettings()
        match export.traces_protocol or export.protocol:
            case OtlpProtocol.GRPC:
                raise ValueError(_GRPC_UNSUPPORTED)
            case OtlpProtocol.HTTP_JSON:
                msg = (
                    "DLBOARD_TELEMETRY_TRACES exports OTLP with protobuf encoding, not JSON: unset "
                    "OTEL_EXPORTER_OTLP_PROTOCOL or set it to http/protobuf"
                )
                raise ValueError(msg)
            case OtlpProtocol.HTTP_PROTOBUF | None:
                return self


def sdk_installed() -> bool:
    """Whether the OpenTelemetry SDK can be imported, without importing it."""
    try:
        return importlib.util.find_spec("opentelemetry.sdk") is not None
    except (ImportError, ValueError):  # `opentelemetry` itself absent (or blocked)
        return False


CLIENT_SERVICE_NAME: Final = "dlboard-client"
"""What a training process and its shipping processes call themselves in a trace."""
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


_owns_process = False


def own_process() -> None:
    """
    Say that this process is dlboard's alone -- the server and its blob writer, never a training process -- before its first span.

    Its tracer then joins the process's OpenTelemetry instead of staying private: it uses the SDK provider a platform
    (`opentelemetry-instrument`, the Kubernetes Operator) already set up, whether or not
    `DLBOARD_TELEMETRY_TRACES` is on, and otherwise, when it is, becomes the global one.
    """
    global _owns_process
    _owns_process = True


def _build_traces() -> Traces | None:
    enabled = TraceSettings().traces
    # Asked without importing anything: a platform that set up OpenTelemetry has already imported its SDK.
    if not enabled and not (_owns_process and "opentelemetry.sdk.trace" in sys.modules):
        return None
    # Deferred: the SDK is only imported once tracing is on.
    from dlboard._telemetry._sdk import build_traces

    return build_traces(_service, enabled=enabled, owns_process=_owns_process)


TRACES: Final = PerProcess(_build_traces)
"""This process's tracer, built on first use (`None` while tracing is off and no platform has set one up)."""

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
    """Run `call` on this process's tracer if it has one -- never building it: all of these need a span already open."""
    try:
        traces = TRACES.peek()
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


def annotate_current(attributes: Attributes) -> None:
    """Add `attributes` to the span open here, which something else (an instrumented library) opened."""
    _traced(lambda traces: traces.annotate(attributes.as_otel()), None)


def fail_current(exc: BaseException) -> None:
    """Record `exc` on the span open here, for an error a handler turns into a response, so no block ever sees it."""
    _traced(lambda traces: traces.fail(exc), None)
