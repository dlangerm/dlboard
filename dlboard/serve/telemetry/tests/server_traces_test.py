"""
A traced server, against the real SDK: a request continues its caller's trace, and the SQL it runs hangs off it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
import requests
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.trace import SpanKind

from dlboard._telemetry.traces import TRACES, TraceKey, flush
from dlboard._wire import REQUEST_ID_HEADER, WHOAMI_PATH, Resource, create_path, get_or_create_path
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend
from dlboard.serve.telemetry._instrument import instrument_libraries

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


def test_a_callbacks_request_names_its_outputs_but_never_what_a_user_named(
    span_exporter: InMemorySpanExporter, backend_server: BackendServer
) -> None:
    """Every callback is one route: its trace says which ran, a pattern-matching output by its `type` alone."""
    requests.post(
        f"{backend_server.url}/_dash-update-component",
        json={
            "output": "unused",
            "outputs": [
                [{"id": {"type": "panel-body", "panel": _SECRET_NAME}, "property": "children"}] * 2,
                {"id": "live-status", "property": "children"},
            ],
            "inputs": [],
            "changedPropIds": [],
        },
        timeout=10,
    )
    flush()

    (server,) = (s for s in span_exporter.get_finished_spans() if s.kind == SpanKind.SERVER)
    assert server.attributes is not None
    assert server.attributes[TraceKey.DASH_OUTPUTS] == "panel-body.children live-status.children"
    assert _SECRET_NAME not in str(server.attributes)


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


@pytest.fixture
def excluded_urls(request: pytest.FixtureRequest, telemetry: pytest.MonkeyPatch) -> None:
    """The user's URL exclusions, set before the app is built, which is when they are read."""
    parameters: dict[str, str] = request.param
    for name, value in parameters.items():
        telemetry.setenv(name, value)


@pytest.mark.parametrize(
    ("excluded_urls", "traced"),
    [
        pytest.param({"OTEL_PYTHON_EXCLUDED_URLS": "whoami"}, False, id="the-generic-variable"),
        pytest.param({"OTEL_PYTHON_FLASK_EXCLUDED_URLS": "whoami"}, False, id="the-flask-variable"),
        pytest.param(
            {"OTEL_PYTHON_EXCLUDED_URLS": "whoami", "OTEL_PYTHON_FLASK_EXCLUDED_URLS": "nothing-matches"},
            True,
            id="the-flask-variable-replaces-the-generic-one",
        ),
        pytest.param({}, True, id="neither"),
    ],
    indirect=["excluded_urls"],
)
def test_a_user_can_keep_urls_out_of_the_traces_with_either_standard_variable(
    excluded_urls: None, span_exporter: InMemorySpanExporter, backend_server: BackendServer, *, traced: bool
) -> None:
    del excluded_urls
    requests.get(f"{backend_server.url}/{WHOAMI_PATH}", timeout=10)
    flush()

    assert bool([s for s in span_exporter.get_finished_spans() if s.kind == SpanKind.SERVER]) is traced


@pytest.mark.parametrize("store_backend", EVERY_STORE_BACKEND)
def test_a_statement_is_one_span_whichever_database_runs_it(
    span_exporter: InMemorySpanExporter, backend_server: BackendServer, store_backend: StoreBackend
) -> None:
    """On Postgres the driver's instrumentation would trace each statement again, beside SQLAlchemy's span for it."""
    del store_backend  # only selects which database `backend_server` runs on
    requests.post(
        f"{backend_server.url}/{get_or_create_path(Resource.PROJECTS)}",
        json={"name": "once", "description": ""},
        timeout=10,
    )
    flush()

    traced_by = {
        s.instrumentation_scope.name
        for s in span_exporter.get_finished_spans()
        if s.instrumentation_scope and s.attributes and "db.statement" in s.attributes
    }
    assert traced_by == {"opentelemetry.instrumentation.sqlalchemy"}


@pytest.mark.parametrize("store_backend", EVERY_STORE_BACKEND)
def test_a_writes_commit_is_traced_after_its_statement_in_the_same_request(
    span_exporter: InMemorySpanExporter, backend_server: BackendServer, store_backend: StoreBackend
) -> None:
    """The SQL instrumentation only sees statements: a commit's round trip is a span of dlboard's own."""
    del store_backend  # only selects which database `backend_server` runs on
    requests.post(
        f"{backend_server.url}/{get_or_create_path(Resource.PROJECTS)}",
        json={"name": "committed", "description": ""},
        timeout=10,
    )
    flush()

    spans = span_exporter.get_finished_spans()
    (request,) = [s for s in spans if s.kind == SpanKind.SERVER]
    assert request.context is not None
    in_request = [
        s for s in spans if s.context is not None and s.context.trace_id == request.context.trace_id
    ]
    insert = next(
        s for s in in_request if str((s.attributes or {}).get("db.statement", "")).startswith("INSERT")
    )
    assert insert.end_time is not None
    assert any(
        s.name == "dlboard.store.commit" and (s.start_time or 0) >= insert.end_time for s in in_request
    )


@pytest.mark.parametrize(
    ("disabled", "driver_traced"),
    [
        pytest.param("", False, id="sqlalchemy-already-traces-every-statement"),
        pytest.param("sqlalchemy", True, id="the-driver-instead-when-sqlalchemys-is-off"),
    ],
)
def test_the_database_driver_is_traced_only_where_sqlalchemy_is_not(
    span_exporter: InMemorySpanExporter, telemetry: pytest.MonkeyPatch, disabled: str, *, driver_traced: bool
) -> None:
    del span_exporter  # only so there is a tracer
    telemetry.setenv("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS", disabled)
    traces = TRACES.get()
    assert traces is not None

    instrument_libraries(traces)

    assert PsycopgInstrumentor().is_instrumented_by_opentelemetry is driver_traced
    assert SQLAlchemyInstrumentor().is_instrumented_by_opentelemetry is not bool(disabled)


def test_an_instrumentor_for_a_library_that_is_not_installed_is_skipped_without_an_error_in_the_log(
    span_exporter: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    """Left to `instrument()`, each of them logs an error, in every worker, for a library nobody was going to use."""
    del span_exporter  # only so there is a tracer
    traces = TRACES.get()
    assert traces is not None

    instrument_libraries(traces)

    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
