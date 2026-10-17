"""
Distributed traces against the real SDK, with an in-memory exporter standing in for the collector.

A fork is a real `os.fork()`: process identity is the point. The server's and the client's use of these spans is
tested where they are made.
"""

from __future__ import annotations

import contextlib
import os
import pickle
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
import structlog
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from dlboard._telemetry import Attributes, attribute, traces
from dlboard._telemetry import _sdk as sdk
from dlboard._telemetry.traces import (
    TRACES,
    SpanKind,
    TraceKey,
    TraceSettings,
    annotate_current,
    carrier,
    fail_current,
    flush,
    name_service,
    span,
    trace_ids,
)

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import SpanContext


@dataclass(frozen=True, slots=True)
class _Where(Attributes):
    run: int = attribute(TraceKey.RUN_ID)
    experiment: int = attribute(TraceKey.EXPERIMENT_ID)


@pytest.fixture
def exported(telemetry: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Tracing on, with every finished span collected in memory (call `flush()` to be sure it has been)."""
    exporter = InMemorySpanExporter()
    telemetry.setenv("DLBOARD_TELEMETRY_TRACES", "true")
    telemetry.setattr(sdk, "make_exporter", lambda: exporter)
    sdk.resource.cache_clear()  # one identity per process, remembered: a test starts over
    return exporter


def _context(finished: ReadableSpan) -> SpanContext:
    assert finished.context is not None
    return finished.context


def _finished(exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    flush()
    return {s.name: s for s in exporter.get_finished_spans()}


def test_with_tracing_off_a_span_runs_its_block_and_leaves_nothing_behind() -> None:
    ran: list[object] = []

    with span("work", SpanKind.INTERNAL, _Where(run=1, experiment=2)) as active:
        active.set(_Where(run=3, experiment=4))
        active.event("noted")
        active.failed(ValueError("handled"))
        ran.append(carrier())

    assert ran == [{}]
    assert trace_ids() is None
    assert TRACES.get() is None


def test_nested_spans_are_one_trace_with_their_attributes_and_events(exported: InMemorySpanExporter) -> None:
    with span("outer", SpanKind.SERVER, _Where(run=1, experiment=2)), span("inner", SpanKind.CLIENT) as inner:
        inner.set(_Where(run=7, experiment=8))
        inner.event("retry", _Where(run=7, experiment=9))
        ids = trace_ids()

    spans = _finished(exported)
    outer, child = spans["outer"], spans["inner"]
    assert child.parent is not None
    assert child.parent.span_id == _context(outer).span_id
    assert _context(child).trace_id == _context(outer).trace_id
    assert (outer.kind.name, child.kind.name) == ("SERVER", "CLIENT")
    assert dict(outer.attributes or {}) == {"dlboard.run.id": 1, "dlboard.experiment.id": 2}
    assert dict(child.attributes or {}) == {"dlboard.run.id": 7, "dlboard.experiment.id": 8}
    assert [(e.name, dict(e.attributes or {})) for e in child.events] == [
        ("retry", {"dlboard.run.id": 7, "dlboard.experiment.id": 9})
    ]
    assert ids == (f"{_context(child).trace_id:032x}", f"{_context(child).span_id:016x}")


def test_a_carrier_continues_a_trace_where_no_library_could(exported: InMemorySpanExporter) -> None:
    with span("request", SpanKind.SERVER):
        handed_over = pickle.loads(pickle.dumps(carrier()))  # what a `multiprocessing` queue does to it
        other = carrier()

    with (
        span("write", SpanKind.CONSUMER, parent=handed_over),
        span("record", SpanKind.INTERNAL, links=[other]),
    ):
        pass

    spans = _finished(exported)
    assert set(handed_over) == {"traceparent"}
    assert spans["write"].parent is not None
    assert spans["write"].parent.span_id == _context(spans["request"]).span_id
    assert _context(spans["write"]).trace_id == _context(spans["request"]).trace_id
    # A link relates the span to the request without being its child.
    assert [link.context.span_id for link in spans["record"].links] == [_context(spans["request"]).span_id]


def test_a_failed_block_records_its_traceback_and_never_its_local_variables(
    exported: InMemorySpanExporter,
) -> None:
    def fail_holding(password: str) -> None:
        msg = "the upload failed"
        raise ConnectionError(msg)

    secret = "-".join(["hunter2", "the", "password"])

    with pytest.raises(ConnectionError), span("upload", SpanKind.CLIENT):
        fail_holding(secret)

    finished = _finished(exported)["upload"]
    (event,) = finished.events
    assert finished.status.status_code is StatusCode.ERROR
    assert event.name == "exception"
    attributes = dict(event.attributes or {})
    assert attributes["exception.type"] == "ConnectionError"
    assert "fail_holding" in str(attributes["exception.stacktrace"])
    assert secret not in repr((finished.attributes, finished.events, finished.status))


def test_tracing_failing_warns_once_and_never_reaches_the_traced_code(
    exported: InMemorySpanExporter, telemetry: pytest.MonkeyPatch
) -> None:
    def start_fails(*_args: object) -> None:
        msg = "tracer on fire"
        raise RuntimeError(msg)

    telemetry.setattr(sdk.Traces, "start", start_fails)
    ran: list[object] = []

    with structlog.testing.capture_logs() as logs:
        for _ in range(2):
            with span("work", SpanKind.INTERNAL) as active:
                active.set(_Where(run=1, experiment=2))
                ran.append(1)

    assert ran == [1, 1]
    assert not exported.get_finished_spans()
    assert [entry["log_level"] for entry in logs] == ["warning"]


def test_a_down_collector_costs_a_bounded_flush_and_never_the_traced_code(
    telemetry: pytest.MonkeyPatch,
) -> None:
    telemetry.setenv("DLBOARD_TELEMETRY_TRACES", "true")
    telemetry.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")  # nothing listens there
    telemetry.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1")
    started = time.monotonic()

    with span("work", SpanKind.INTERNAL):
        pass
    flush()

    assert time.monotonic() - started < 8  # `flush` waits for the collector no longer than its own limit


@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # fork() in a process with threads
def test_a_forked_child_builds_its_own_tracer_with_its_own_identity(exported: InMemorySpanExporter) -> None:
    with span("parent", SpanKind.INTERNAL):
        pass
    parent_id = _finished(exported)["parent"].resource.attributes["service.instance.id"]
    read, write = os.pipe()

    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            os.close(read)
            with span("child", SpanKind.INTERNAL):
                pass
            flush()
            child = {s.name: s for s in exported.get_finished_spans()}["child"]
            os.write(write, str(child.resource.attributes["service.instance.id"]).encode())
            code = 0
        finally:
            os._exit(code)
    os.close(write)
    child_id = os.read(read, 64).decode()
    _, status = os.waitpid(pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert child_id
    assert child_id != parent_id


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        pytest.param(None, "dlboard-client", id="the-name-a-process-gives-itself"),
        pytest.param("mine", "mine", id="otel-service-name"),
    ],
)
def test_a_process_is_named_in_its_traces_unless_the_user_names_it(
    exported: InMemorySpanExporter, telemetry: pytest.MonkeyPatch, override: str | None, expected: str
) -> None:
    telemetry.setattr(traces, "_service", "dlboard")  # put back afterwards
    if override is not None:
        telemetry.setenv("OTEL_SERVICE_NAME", override)
    name_service("dlboard-client")

    with span("work", SpanKind.INTERNAL):
        pass

    assert _finished(exported)["work"].resource.attributes["service.name"] == expected


def test_a_span_something_else_opened_can_be_annotated_and_failed(exported: InMemorySpanExporter) -> None:
    with span("library", SpanKind.SERVER):
        annotate_current(_Where(run=1, experiment=2))
        fail_current(ValueError("answered with a 500"))

    finished = _finished(exported)["library"]
    assert finished.attributes is not None
    assert finished.attributes[TraceKey.RUN_ID] == 1
    assert finished.status.status_code == StatusCode.ERROR
    assert [e.name for e in finished.events] == ["exception"]


def test_reading_the_open_span_never_builds_a_tracer(exported: InMemorySpanExporter) -> None:
    """Asking for ids or a carrier happens in places (a log line, the main process) that must not start one."""
    assert trace_ids() is None
    assert carrier() == {}
    flush()

    assert TRACES.peek() is None
    assert not exported.get_finished_spans()


@pytest.mark.parametrize(
    ("env", "traces_on", "message"),
    [
        pytest.param({"OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}, True, "4318", id="grpc-for-everything"),
        pytest.param({"OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "grpc"}, True, "4318", id="grpc-for-traces"),
        pytest.param({"OTEL_EXPORTER_OTLP_PROTOCOL": "http/json"}, True, "protobuf", id="http-json"),
        pytest.param(
            {"OTEL_EXPORTER_OTLP_PROTOCOL": "grpc", "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf"},
            True,
            None,
            id="the-traces-setting-wins",
        ),
        pytest.param({"OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf"}, True, None, id="http-protobuf"),
        pytest.param({}, True, None, id="unset"),
        pytest.param({"OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}, False, None, id="not-asked-to-trace"),
    ],
)
def test_a_protocol_the_exporter_cannot_speak_stops_startup_instead_of_losing_every_span(
    telemetry: pytest.MonkeyPatch, env: dict[str, str], *, traces_on: bool, message: str | None
) -> None:
    telemetry.setenv("DLBOARD_TELEMETRY_TRACES", str(traces_on).lower())
    for name, value in env.items():
        telemetry.setenv(name, value)

    with pytest.raises(ValueError, match=message) if message else contextlib.nullcontext():
        TraceSettings()
