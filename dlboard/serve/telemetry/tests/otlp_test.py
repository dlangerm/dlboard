"""What actually goes over the wire to an OTLP collector: a sink that records and decodes the requests."""

from __future__ import annotations

import importlib.metadata
from typing import TYPE_CHECKING

import pytest
import structlog
from opentelemetry._logs import SeverityNumber
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.logs.v1.logs_pb2 import SeverityNumber as WireSeverity

from dlboard._telemetry import traces
from dlboard._telemetry.traces import OtlpExportSettings, OtlpProtocol, OtlpSignal, SpanKind, span, trace_ids
from dlboard.conftest import otlp_collector, otlp_protocol
from dlboard.serve._logging import configure_logging
from dlboard.serve.telemetry import LOGS, METRICS, Counter, NoAttributes, TelemetrySettings, Unit
from dlboard.serve.telemetry._sdk import _severity, forward_log

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_ACCEPTED = Counter[NoAttributes]("test.accepted", Unit.ARTIFACTS, "Counted by a test.")


class _Sink:
    """A collector's view: the last request it was sent of each signal, by its HTTP path."""

    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}

    def receive(self, path: str, body: bytes) -> None:
        self.bodies[path] = body


@pytest.fixture
def collector(telemetry: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[_Sink]:
    """Metrics and logs on, exported to a collector listening here: over OTLP/HTTP, unless parametrized otherwise."""
    structlog.reset_defaults()  # `configure_logging` only configures once per process
    sink = _Sink()
    protocol = otlp_protocol(request)
    with otlp_collector(protocol, sink.receive) as endpoint:
        telemetry.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
        telemetry.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", protocol.value)
        telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "otlp")
        telemetry.setenv("DLBOARD_TELEMETRY_LOGS", "true")
        yield sink
    structlog.reset_defaults()


@pytest.mark.parametrize(
    "collector", [OtlpProtocol.HTTP_PROTOBUF, OtlpProtocol.GRPC], indirect=True, ids=["http", "grpc"]
)
def test_metrics_and_logs_reach_a_collector_with_this_processs_identity(collector: _Sink) -> None:
    configure_logging()
    _ACCEPTED.add(3)
    try:
        msg = "disk on fire"
        raise OSError(msg)  # noqa: TRY301
    except OSError:
        structlog.get_logger().exception("write failed", run_id=7)
    METRICS.close()  # flushes, as a worker's exit does
    LOGS.close()

    metrics = ExportMetricsServiceRequest.FromString(collector.bodies["/v1/metrics"]).resource_metrics[0]
    resource = {a.key: a.value.string_value for a in metrics.resource.attributes}
    (counter,) = (m for scope in metrics.scope_metrics for m in scope.metrics if m.name == "test.accepted")
    assert counter.sum.data_points[0].as_int == 3
    assert resource["service.name"] == "dlboard"
    assert resource["service.instance.id"]
    (record,) = (
        ExportLogsServiceRequest.FromString(collector.bodies["/v1/logs"])
        .resource_logs[0]
        .scope_logs[0]
        .log_records
    )
    attributes = {a.key: a.value.string_value or a.value.int_value for a in record.attributes}
    assert record.body.string_value == "write failed"
    assert record.severity_number == WireSeverity.SEVERITY_NUMBER_ERROR
    assert attributes["run_id"] == 7
    assert attributes["exception.type"] == "OSError"
    assert attributes["exception.message"] == "disk on fire"


@pytest.mark.parametrize(
    ("level", "severity"),
    [
        ("debug", SeverityNumber.DEBUG),
        ("info", SeverityNumber.INFO),
        ("warning", SeverityNumber.WARN),
        ("error", SeverityNumber.ERROR),
        ("critical", SeverityNumber.FATAL),
        ("a custom level", SeverityNumber.UNSPECIFIED),
    ],
)
def test_a_log_level_maps_to_its_severity(level: str, severity: SeverityNumber) -> None:
    assert _severity(level) is severity


def test_forwarding_a_log_never_raises_so_logging_never_fails_because_telemetry_did(
    collector: _Sink, telemetry: pytest.MonkeyPatch
) -> None:
    def fail() -> None:
        msg = "logger provider on fire"
        raise RuntimeError(msg)

    telemetry.setattr(LOGS, "get", fail)

    event: dict[str, object] = {"event": "hello", "level": "info"}
    assert forward_log(None, "info", event) is event


@pytest.mark.parametrize(("installed", "accepted"), [("1.37.0", False), ("1.38.0", True), ("1.45.1", True)])
def test_logs_need_a_newer_sdk_than_metrics_do(
    telemetry: pytest.MonkeyPatch, installed: str, accepted: bool
) -> None:
    def version(_name: str) -> str:
        return installed

    telemetry.setattr(importlib.metadata, "version", version)
    telemetry.setenv("DLBOARD_TELEMETRY_LOGS", "true")

    if accepted:
        assert TelemetrySettings().logs
    else:
        with pytest.raises(ValueError, match=r"opentelemetry-sdk>=1\.38"):
            TelemetrySettings()


def test_a_log_record_carries_its_spans_ids_in_its_own_fields_and_not_again_as_attributes(
    collector: _Sink, span_exporter: InMemorySpanExporter
) -> None:
    """A collector turns attributes into structured metadata: ids there as well would be stored twice."""
    del span_exporter  # only so there is a tracer, and so a span to be inside
    configure_logging()

    with span("work", SpanKind.INTERNAL):
        structlog.get_logger().info("inside")
        ids = trace_ids()
    LOGS.close()

    (record,) = (
        ExportLogsServiceRequest.FromString(collector.bodies["/v1/logs"])
        .resource_logs[0]
        .scope_logs[0]
        .log_records
    )
    assert ids is not None
    assert (record.trace_id.hex(), record.span_id.hex()) == ids
    assert {"trace_id", "span_id"}.isdisjoint(a.key for a in record.attributes)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"DLBOARD_TELEMETRY_METRICS": "otlp", "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL": "grpc"}, "metrics"),
        ({"DLBOARD_TELEMETRY_LOGS": "true", "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}, "logs"),
        ({"DLBOARD_TELEMETRY_LOGS": "true", "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "http/json"}, "not JSON"),
    ],
    ids=["grpc-metrics-without-the-extra", "grpc-logs-without-the-extra", "json-logs"],
)
def test_a_protocol_a_signal_cannot_be_sent_by_stops_startup(
    telemetry: pytest.MonkeyPatch, env: dict[str, str], message: str
) -> None:
    """Each signal's own protocol is checked (#300): gRPC without the `otel-grpc` extra, or JSON, says so at startup."""
    for name, value in env.items():
        telemetry.setenv(name, value)
    telemetry.setattr(traces, "grpc_exporter_installed", lambda: False)

    with pytest.raises(ValueError, match=message):
        TelemetrySettings()


def test_each_signal_is_sent_by_its_own_protocol_or_every_signals(telemetry: pytest.MonkeyPatch) -> None:
    telemetry.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    telemetry.setenv("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL", "http/protobuf")

    export = OtlpExportSettings()

    assert [export.of(signal) for signal in OtlpSignal] == [
        OtlpProtocol.GRPC,
        OtlpProtocol.GRPC,
        OtlpProtocol.HTTP_PROTOBUF,
    ]
