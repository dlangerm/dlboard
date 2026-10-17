"""
How a process's tracer sits among the OpenTelemetry around it: a platform's provider, the global one, the wire format,
and how long exit may take.

The state under test is process-wide (the global provider is set once, ever), so each test starts it over.
"""

from __future__ import annotations

import io
import logging
import time
from typing import TYPE_CHECKING, override

import pytest
from opentelemetry import propagate, trace
from opentelemetry.propagators.textmap import (
    CarrierT,
    Getter,
    Setter,
    TextMapPropagator,
    default_getter,
    default_setter,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from opentelemetry.util._once import Once

from dlboard._telemetry import _sdk as sdk
from dlboard._telemetry import traces
from dlboard._telemetry.traces import TRACES, SpanKind, carrier, flush, own_process, span

if TYPE_CHECKING:
    from opentelemetry.context import Context


@pytest.fixture
def fresh_process(telemetry: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No global provider, no memory of having installed one, and a process that is nobody's in particular."""
    telemetry.setattr(trace, "_TRACER_PROVIDER", None)
    telemetry.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    telemetry.setattr(sdk, "_installed_here", None)
    telemetry.setattr(traces, "_owns_process", False)
    sdk.resource.cache_clear()
    return telemetry


def test_a_client_keeps_its_tracer_private_and_leaves_the_users_opentelemetry_alone(
    fresh_process: pytest.MonkeyPatch, span_exporter: InMemorySpanExporter
) -> None:
    fresh_process.setattr(logging.getLogger("opentelemetry"), "level", logging.WARNING)

    with span("work", SpanKind.INTERNAL):
        pass

    flush()
    assert [s.name for s in span_exporter.get_finished_spans()] == ["work"]
    assert isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider), (
        "no global provider was installed"
    )
    assert logging.getLogger("opentelemetry").level == logging.WARNING, (
        "the user's own warnings are still shown"
    )


def test_a_dlboard_only_process_makes_its_tracer_the_global_one_so_a_plugin_reaches_it(
    fresh_process: pytest.MonkeyPatch, span_exporter: InMemorySpanExporter
) -> None:
    del fresh_process
    own_process()

    with (
        span("dlboard work", SpanKind.INTERNAL),
        trace.get_tracer("a plugin").start_as_current_span("plugin work"),
    ):
        pass

    flush()
    spans = {s.name: s for s in span_exporter.get_finished_spans()}
    assert set(spans) == {"dlboard work", "plugin work"}
    assert spans["plugin work"].parent is not None
    assert spans["plugin work"].resource == spans["dlboard work"].resource, "one identity for both"


def test_a_platforms_provider_is_joined_and_left_running_whether_or_not_dlboards_tracing_is_on(
    fresh_process: pytest.MonkeyPatch,
) -> None:
    """`opentelemetry-instrument` and the Kubernetes Operator set a provider up first; two pipelines would be wrong."""
    del fresh_process
    platform_exports = InMemorySpanExporter()
    platform = TracerProvider()
    platform.add_span_processor(SimpleSpanProcessor(platform_exports))
    trace.set_tracer_provider(platform)
    own_process()

    with span("dlboard work", SpanKind.INTERNAL):
        pass
    TRACES.close()  # the process is exiting: dlboard lets go of the provider, it does not shut it down
    with platform.get_tracer("the platform").start_as_current_span("later"):
        pass

    assert [s.name for s in platform_exports.get_finished_spans()] == ["dlboard work", "later"]


def test_with_tracing_off_and_no_platform_a_dlboard_only_process_still_traces_nothing(
    fresh_process: pytest.MonkeyPatch,
) -> None:
    del fresh_process
    own_process()

    with span("work", SpanKind.INTERNAL):
        pass

    assert TRACES.get() is None


class _OneHeader(TextMapPropagator):
    """A wire format that is not W3C: what a mesh standardised on `b3` or `xray` looks like to dlboard."""

    @override
    def inject(
        self, carrier: CarrierT, context: Context | None = None, setter: Setter[CarrierT] = default_setter
    ) -> None:
        ids = trace.get_current_span(context).get_span_context()
        setter.set(carrier, "x-trace", f"{ids.trace_id:032x}-{ids.span_id:016x}")

    @override
    def extract(
        self, carrier: CarrierT, context: Context | None = None, getter: Getter[CarrierT] = default_getter
    ) -> Context:
        (value,) = getter.get(carrier, "x-trace") or [""]
        trace_id, span_id = (int(part, 16) for part in value.split("-"))
        remote = SpanContext(trace_id, span_id, is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED))
        return trace.set_span_in_context(NonRecordingSpan(remote), context)

    @property
    def fields(self) -> set[str]:
        return {"x-trace"}


def test_the_trace_crosses_a_process_in_the_wire_format_otel_propagators_names(
    span_exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(propagate, "_HTTP_TEXT_FORMAT", propagate.get_global_textmap())  # put back afterwards
    propagate.set_global_textmap(_OneHeader())

    with span("client", SpanKind.CLIENT):
        headers = carrier()
    with span("server", SpanKind.SERVER, parent=headers):
        pass
    flush()

    finished = {s.name: s for s in span_exporter.get_finished_spans()}
    assert set(headers) == {"x-trace"}
    assert finished["server"].context is not None
    assert finished["client"].context is not None
    assert finished["server"].context.trace_id == finished["client"].context.trace_id
    assert finished["server"].parent is not None
    assert finished["server"].parent.span_id == finished["client"].context.span_id


def test_exit_waits_no_longer_than_a_bound_for_a_collector_that_will_not_let_go(
    span_exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider's own shutdown joins the export thread with no timeout, and that thread may be backing off."""
    del span_exporter

    class _Stuck(InMemorySpanExporter):
        @override
        def shutdown(self) -> None:
            time.sleep(10)

    monkeypatch.setattr(sdk, "make_exporter", _Stuck)
    monkeypatch.setattr(sdk, "_FLUSH_TIMEOUT_MS", 200)
    with span("work", SpanKind.INTERNAL):
        pass

    started = time.monotonic()
    TRACES.close()

    assert time.monotonic() - started < 3


def test_with_the_exporter_none_spans_still_exist_and_travel_but_nothing_is_exported(
    span_exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")

    with span("work", SpanKind.INTERNAL):
        headers = carrier()
    flush()

    assert headers, "the trace still crosses to whoever receives it"
    assert not span_exporter.get_finished_spans()


def test_with_the_exporter_console_spans_are_written_out_instead_of_sent(
    span_exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = io.StringIO()
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
    monkeypatch.setattr(sdk, "ConsoleSpanExporter", lambda: ConsoleSpanExporter(out=written))

    with span("work", SpanKind.INTERNAL):
        pass
    flush()

    assert '"name": "work"' in written.getvalue()
    assert not span_exporter.get_finished_spans(), "nothing was sent to OTLP"
