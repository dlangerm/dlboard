"""What actually goes over the wire to an OTLP collector: a sink that records and decodes the requests."""

from __future__ import annotations

import importlib.metadata
import threading
from typing import TYPE_CHECKING

import pytest
import structlog
from opentelemetry._logs import SeverityNumber
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.logs.v1.logs_pb2 import SeverityNumber as WireSeverity
from werkzeug.serving import make_server

from dlboard.serve._logging import configure_logging
from dlboard.serve.telemetry import LOGS, METRICS, Counter, NoAttributes, TelemetrySettings, Unit
from dlboard.serve.telemetry._sdk import _severity, forward_log

if TYPE_CHECKING:
    from collections.abc import Iterator
    from wsgiref.types import InputStream, StartResponse, WSGIEnvironment

_ACCEPTED = Counter[NoAttributes]("test.accepted", Unit.ARTIFACTS, "Counted by a test.")


class _Sink:
    """A collector that answers 200 to everything and keeps what it was sent, by path."""

    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> list[bytes]:
        stream: InputStream = environ["wsgi.input"]
        self.bodies[environ["PATH_INFO"]] = stream.read(int(environ["CONTENT_LENGTH"]))
        start_response("200 OK", [("Content-Type", "application/x-protobuf")])
        return [b""]


@pytest.fixture
def collector(telemetry: pytest.MonkeyPatch) -> Iterator[_Sink]:
    structlog.reset_defaults()  # `configure_logging` only configures once per process
    sink = _Sink()
    server = make_server("127.0.0.1", 0, sink)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    telemetry.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
    telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "otlp")
    telemetry.setenv("DLBOARD_TELEMETRY_LOGS", "true")
    yield sink
    server.shutdown()
    thread.join()
    structlog.reset_defaults()


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


@pytest.mark.parametrize(("installed", "accepted"), [("1.36.0", False), ("1.37.0", True), ("1.45.1", True)])
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
        with pytest.raises(ValueError, match=r"opentelemetry-sdk>=1\.37"):
            TelemetrySettings()
