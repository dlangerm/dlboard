"""
A traced server, against the real SDK: a request continues its caller's trace, and the SQL it runs hangs off it.

The instrumentation is process-wide (it wraps `create_engine`), so the fixture undoes it after each test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import requests
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from dlboard._telemetry import _sdk
from dlboard._telemetry.traces import TraceKey, flush
from dlboard._wire import REQUEST_ID_HEADER, Resource, get_or_create_path
from dlboard.serve.telemetry import _instrument

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dlboard.conftest import BackendServer

_TRACE_ID = 0x4BF92F3577B34DA6A3CE929D0E0E4736
_TRACEPARENT = f"00-{_TRACE_ID:032x}-00f067aa0ba902b7-01"
_SECRET_NAME = "a-name-that-must-not-be-bound-into-a-span"


@pytest.fixture
def exported(telemetry: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    telemetry.setenv("DLBOARD_TELEMETRY_TRACES", "true")
    telemetry.setattr(_sdk, "make_exporter", lambda: exporter)
    _sdk.resource.cache_clear()
    yield exporter
    _instrument.uninstrument_libraries()


def test_a_request_continues_its_callers_trace_and_its_sql_hangs_off_it(
    exported: InMemorySpanExporter, backend_server: BackendServer
) -> None:
    response = requests.post(
        f"{backend_server.url}/{get_or_create_path(Resource.PROJECTS)}",
        json={"name": _SECRET_NAME, "description": ""},
        headers={"traceparent": _TRACEPARENT, REQUEST_ID_HEADER: "req-1"},
        timeout=10,
    )
    response.raise_for_status()
    flush()

    spans = [s for s in exported.get_finished_spans() if s.context and s.context.trace_id == _TRACE_ID]
    (server,) = (s for s in spans if s.kind == SpanKind.SERVER)
    sql = [s for s in spans if s.attributes and "db.system" in s.attributes]
    assert server.attributes is not None
    assert server.attributes[TraceKey.REQUEST_ID] == "req-1"
    assert sql, "the request ran SQL, which should be traced"
    assert all(_SECRET_NAME not in str(s.attributes) for s in spans), "bound parameters must not reach a span"


def test_a_request_to_a_health_probe_is_not_traced(
    exported: InMemorySpanExporter, backend_server: BackendServer
) -> None:
    requests.get(f"{backend_server.url}/healthz", headers={"traceparent": _TRACEPARENT}, timeout=10)
    flush()

    assert [s for s in exported.get_finished_spans() if s.kind == SpanKind.SERVER] == []
