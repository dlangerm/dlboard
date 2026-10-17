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
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from opentelemetry import context, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Link, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard._telemetry.traces import SpanKind
from dlboard._version import __version__

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from contextvars import Token

    from opentelemetry.context import Context
    from opentelemetry.trace import Span, Tracer
    from opentelemetry.util.types import AttributeValue

    from dlboard._telemetry.traces import Carrier

_FLUSH_TIMEOUT_MS: Final = 5000
"""How long `flush` may wait for the collector: a down endpoint must not hold up a shutdown."""

_PROPAGATOR: Final = TraceContextTextMapPropagator()
"""W3C `traceparent`: the one wire format, so any OpenTelemetry (or any other W3C-aware) service can join a trace."""

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
    # A down collector retries quietly; only the final "failed to export" error is worth a line.
    logging.getLogger("opentelemetry").setLevel(logging.ERROR)
    return Resource.create(
        {
            "service.name": OtelResourceSettings().service_name or default_service_name,
            "service.instance.id": uuid4().hex,
            "service.version": __version__,
        }
    )


def _context_of(carrier: Carrier) -> Context:
    return _PROPAGATOR.extract(carrier)


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

    def start(
        self,
        name: str,
        kind: SpanKind,
        attributes: Mapping[str, AttributeValue] | None,
        parent: Carrier | None,
        links: Sequence[Carrier],
    ) -> Opened:
        """Start a span as the current one: a child of `parent`'s context if given, else of the one open here."""
        parent_context = _context_of(parent) if parent else context.get_current()
        span = self.tracer.start_span(
            name,
            context=parent_context,
            kind=_KINDS[kind],
            attributes=None if attributes is None else dict(attributes),
            links=[
                Link(trace.get_current_span(_context_of(link)).get_span_context()) for link in links if link
            ],
        )
        return Opened(span, context.attach(trace.set_span_in_context(span, parent_context)))

    def carrier(self) -> Carrier:
        """The current context as `traceparent` headers (none if no span is open)."""
        headers: Carrier = {}
        _PROPAGATOR.inject(headers)
        return headers

    def ids(self) -> tuple[str, str] | None:
        """The open span's trace and span ids, as hex."""
        span_context = trace.get_current_span().get_span_context()
        if not span_context.is_valid:
            return None
        return trace.format_trace_id(span_context.trace_id), trace.format_span_id(span_context.span_id)

    def flush(self) -> None:
        """Export what has finished, waiting no longer than `_FLUSH_TIMEOUT_MS`."""
        self.provider.force_flush(_FLUSH_TIMEOUT_MS)

    def close(self) -> None:
        """Flush and shut down."""
        self.provider.shutdown()


def build_traces(default_service_name: str) -> Traces:
    """A tracer provider that batches spans and pushes them over OTLP, private to this process (never global)."""
    provider = TracerProvider(
        resource=resource(os.getpid(), default_service_name),
        shutdown_on_exit=False,  # `PerProcess` closes it, where a worker's exit actually runs
    )
    provider.add_span_processor(BatchSpanProcessor(make_exporter()))
    return Traces(provider, provider.get_tracer("dlboard", __version__))
