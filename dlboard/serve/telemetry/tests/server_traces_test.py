"""
A traced server, against the real SDK: a request continues its caller's trace, and the SQL it runs hangs off it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import requests
from opentelemetry.trace import SpanKind

from dlboard._telemetry.traces import TraceKey, flush
from dlboard._wire import REQUEST_ID_HEADER, WHOAMI_PATH, Resource, create_path, get_or_create_path

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from dlboard.conftest import BackendServer

_TRACE_ID = 0x4BF92F3577B34DA6A3CE929D0E0E4736
_TRACEPARENT = f"00-{_TRACE_ID:032x}-00f067aa0ba902b7-01"
_SECRET_NAME = "a-name-that-must-not-be-bound-into-a-span"


def test_a_request_continues_its_callers_trace_and_its_sql_hangs_off_it(
    span_exporter: InMemorySpanExporter, backend_server: BackendServer
) -> None:
    response = requests.post(
        f"{backend_server.url}/{get_or_create_path(Resource.PROJECTS)}",
        json={"name": _SECRET_NAME, "description": ""},
        headers={"traceparent": _TRACEPARENT, REQUEST_ID_HEADER: "req-1"},
        timeout=10,
    )
    response.raise_for_status()
    flush()

    spans = [s for s in span_exporter.get_finished_spans() if s.context and s.context.trace_id == _TRACE_ID]
    (server,) = (s for s in spans if s.kind == SpanKind.SERVER)
    sql = [s for s in spans if s.attributes and "db.system" in s.attributes]
    assert server.attributes is not None
    assert server.attributes[TraceKey.REQUEST_ID] == "req-1"
    # The client writes the stable HTTP names; one trace must not speak two vocabularies.
    assert server.attributes["http.request.method"] == "POST"
    assert "http.method" not in server.attributes
    assert sql, "the request ran SQL, which should be traced"
    assert all(_SECRET_NAME not in str(s.attributes) for s in spans), "bound parameters must not reach a span"


@pytest.mark.parametrize(
    ("path", "traced"),
    [
        pytest.param("healthz", False, id="a-health-probe"),
        pytest.param("metrics", False, id="the-prometheus-scrape"),
        pytest.param(create_path(Resource.METRICS), True, id="the-metric-ingest-route-that-shares-its-name"),
    ],
)
def test_only_the_requests_somebody_looks_at_are_traced(
    span_exporter: InMemorySpanExporter, backend_server: BackendServer, path: str, *, traced: bool
) -> None:
    requests.request("POST" if traced else "GET", f"{backend_server.url}/{path}", json=[], timeout=10)
    flush()

    assert bool([s for s in span_exporter.get_finished_spans() if s.kind == SpanKind.SERVER]) is traced


@pytest.fixture
def _records_the_authorization_header(telemetry: pytest.MonkeyPatch) -> None:
    """What a user does to record a header: set before the app is built, which is when dlboard adds its redactions."""
    telemetry.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST", "authorization")
    # dlboard sets the sanitize list in the process environment itself: have it put back as it was afterwards.
    telemetry.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS", "")
    telemetry.delenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS")


@pytest.mark.usefixtures("_records_the_authorization_header")
def test_a_credential_in_a_header_a_user_chose_to_record_is_redacted(
    span_exporter: InMemorySpanExporter, backend_server: BackendServer
) -> None:
    """Headers are never recorded unless asked for -- and then not the ones a credential travels in."""
    requests.get(
        f"{backend_server.url}/{WHOAMI_PATH}", headers={"Authorization": "Bearer dlb_a-secret"}, timeout=10
    )
    flush()

    (server,) = (s for s in span_exporter.get_finished_spans() if s.kind == SpanKind.SERVER)
    assert server.attributes is not None
    assert server.attributes["http.request.header.authorization"] == ("[REDACTED]",)
    assert "dlb_a-secret" not in str(server.attributes)
