"""
The OpenTelemetry SDK half of `dlboard._telemetry.traces` -- imported only once tracing is switched on.

Used by the client as well as the server, so it calls only OpenTelemetry API that exists in the oldest release the
client's `otel` extra allows (1.16); CI installs exactly that release and runs the same calls. Nothing newer.

Exporter endpoint, headers, timeouts, sampler and the rest are the SDK's own `OTEL_EXPORTER_OTLP_*` /
`OTEL_TRACES_SAMPLER` / `OTEL_RESOURCE_ATTRIBUTES` variables; nothing here re-implements them.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from opentelemetry import context, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import Link, Status, StatusCode
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard._telemetry.traces import SpanKind, TracesExporter, TracesExporterSettings
from dlboard._version import __version__

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from contextvars import Token

    from opentelemetry.trace import Span, Tracer
    from opentelemetry.util.types import AttributeValue

    from dlboard._telemetry.traces import Carrier

_FLUSH_TIMEOUT_MS: Final = 5000
"""How long `flush` may wait for the collector: a down endpoint must not hold up a shutdown."""

_KINDS: Final = {
    SpanKind.INTERNAL: trace.SpanKind.INTERNAL,
    SpanKind.CLIENT: trace.SpanKind.CLIENT,
    SpanKind.SERVER: trace.SpanKind.SERVER,
    SpanKind.PRODUCER: trace.SpanKind.PRODUCER,
    SpanKind.CONSUMER: trace.SpanKind.CONSUMER,
}


def make_exporter() -> OTLPSpanExporter:
    """The exporter every process's tracer sends to. A function so a test can put its own in its place."""
    return OTLPSpanExporter()


class OtelResourceSettings(BaseSettings):
    """The standard `OTEL_SERVICE_NAME`, which names the service in everything dlboard exports."""

    model_config = SettingsConfigDict(env_prefix="OTEL_")

    service_name: str | None = None
    """The `service.name` everything is reported under, in place of the one this process would use."""


@cache
def resource(pid: int, default_service_name: str) -> Resource:  # noqa: ARG001 -- the pid is the cache key: one identity per process, shared by every signal
    """
    Who is exporting: a `service.instance.id` unique to this process.

    Every process exports on its own, so they must not share an identity or a collector would see one series
    jumping around. Set in code, so it overrides `OTEL_RESOURCE_ATTRIBUTES`.
    """
    return Resource.create(
        {
            "service.name": OtelResourceSettings().service_name or default_service_name,
            "service.instance.id": uuid4().hex,
            "service.version": __version__,
        }
    )


def quiet_exporter_retries() -> None:
    """
    Log only an exporter's final "failed to export", not every retry of a down collector.

    The `opentelemetry` logger is process-wide, so only a process dlboard owns (the server) does this: a training
    process's OpenTelemetry warnings are the user's to see.
    """
    logging.getLogger("opentelemetry").setLevel(logging.ERROR)


@dataclass(frozen=True)
class Opened:
    """One span that has been started and is the current one in this thread, until it is ended."""

    span: Span
    token: Token[Context]

    def set(self, attributes: Mapping[str, AttributeValue]) -> None:
        """Add `attributes`."""
        self.span.set_attributes(dict(attributes))

    def event(self, name: str, attributes: Mapping[str, AttributeValue] | None) -> None:
        """Record something that happened at this moment."""
        self.span.add_event(name, None if attributes is None else dict(attributes))

    def fail(self, exc: BaseException) -> None:
        """Mark failed. The exception's type, message and traceback text are recorded -- never local variables."""
        self.span.record_exception(exc)
        self.span.set_status(Status(StatusCode.ERROR, type(exc).__name__))

    def end(self) -> None:
        """Stop being the current span, and finish."""
        context.detach(self.token)
        self.span.end()


@dataclass(frozen=True)
class Traces:
    """The current process's tracer, and the provider that exports what it records."""

    provider: TracerProvider
    tracer: Tracer
    owned: bool = True
    """Whether dlboard built `provider`. One it joined belongs to whoever set it up, and is not dlboard's to shut down."""

    def start(
        self,
        name: str,
        kind: SpanKind,
        attributes: Mapping[str, AttributeValue] | None,
        parent: Carrier | None,
        links: Sequence[Carrier],
    ) -> Opened:
        """Start a span as the current one: a child of `parent`'s context if given, else of the one open here."""
        # Extracted onto an empty context: a carrier that names nothing must not turn into "whatever is open here".
        parent_context = extract(parent, Context()) if parent else context.get_current()
        span = self.tracer.start_span(
            name,
            context=parent_context,
            kind=_KINDS[kind],
            attributes=None if attributes is None else dict(attributes),
            links=[
                Link(trace.get_current_span(extract(link, Context())).get_span_context())
                for link in links
                if link
            ],
        )
        return Opened(span, context.attach(trace.set_span_in_context(span, parent_context)))

    def carrier(self) -> Carrier:
        """
        The current context in the wire format `OTEL_PROPAGATORS` names (W3C `traceparent` unless told otherwise).

        Through the API's propagate module, so a mesh that standardises on `b3` or `xray` joins the trace, and the
        server's Flask extraction, which uses the same, reads what the client wrote. Nothing is installed globally.
        """
        headers: Carrier = {}
        inject(headers)
        return headers

    def ids(self) -> tuple[str, str] | None:
        """The open span's trace and span ids, as hex."""
        span_context = trace.get_current_span().get_span_context()
        if not span_context.is_valid:
            return None
        return trace.format_trace_id(span_context.trace_id), trace.format_span_id(span_context.span_id)

    def annotate(self, attributes: Mapping[str, AttributeValue]) -> None:
        """Add `attributes` to the span open here."""
        trace.get_current_span().set_attributes(dict(attributes))

    def fail(self, exc: BaseException) -> None:
        """Record `exc` on the span open here, and mark it failed."""
        span = trace.get_current_span()
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, type(exc).__name__))

    def flush(self) -> None:
        """Export what has finished, waiting no longer than `_FLUSH_TIMEOUT_MS`."""
        self.provider.force_flush(_FLUSH_TIMEOUT_MS)

    def close(self) -> None:
        """
        Flush, then shut down what dlboard built -- waiting no longer than twice `_FLUSH_TIMEOUT_MS` for all of it.

        The provider's own `shutdown` joins its export thread without a timeout, and that thread may be in the middle
        of backing off from a collector that is rate-limiting (old exporters retry for about a minute). Run in a
        thread of its own it can be given up on: the export thread is a daemon, so abandoning it costs the spans it
        was holding, never the exit of the process that is being traced.
        """
        self.flush()
        if self.owned:
            shutdown = threading.Thread(target=self.provider.shutdown, daemon=True)
            shutdown.start()
            shutdown.join(_FLUSH_TIMEOUT_MS / 1000)


_installed_here: TracerProvider | None = None
"""The provider this module made the process's global one, so a later build can tell it from a platform's own."""


def build_traces(default_service_name: str, *, enabled: bool, owns_process: bool) -> Traces | None:
    """
    The tracer for this process: its provider, which batches spans and pushes them over OTLP, if `enabled`.

    A client process (`owns_process` false) gets a provider of its own and never a global one: the process is the
    user's, and their OpenTelemetry setup (or none) is not ours to replace. The server and its writer
    (`owns_process`) are dlboard's alone, so there the tracer joins the process's OpenTelemetry. If a platform
    (`opentelemetry-instrument`, the Kubernetes Operator) already set up an SDK provider, its is used, switched on or
    not, so dlboard's spans and the libraries' leave by one exporter under one identity. Otherwise, when `enabled`,
    dlboard's becomes the global one, and a plugin's own `trace.get_tracer(...)` reaches it too.
    """
    global _installed_here
    current = trace.get_tracer_provider()
    if owns_process:
        quiet_exporter_retries()
        if isinstance(current, TracerProvider) and current is not _installed_here:
            return Traces(current, current.get_tracer("dlboard", __version__), owned=False)
    if not enabled:
        return None
    provider = TracerProvider(
        resource=resource(os.getpid(), default_service_name),
        shutdown_on_exit=False,  # `PerProcess` closes it, where a worker's exit actually runs
    )
    exporters = TracesExporterSettings().traces_exporter
    if TracesExporter.NONE not in exporters:
        if not exporters.isdisjoint({TracesExporter.OTLP, TracesExporter.OTLP_PROTO_HTTP}):
            provider.add_span_processor(BatchSpanProcessor(make_exporter()))
        if TracesExporter.CONSOLE in exporters:
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    if owns_process and not isinstance(current, TracerProvider):
        trace.set_tracer_provider(provider)
        _installed_here = provider
    return Traces(provider, provider.get_tracer("dlboard", __version__))
