"""What actually goes over the wire to an OTLP collector: a sink that records and decodes the requests."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from werkzeug.serving import make_server

from dlboard.serve.telemetry import METRICS, Counter, NoAttributes, Unit

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
    sink = _Sink()
    server = make_server("127.0.0.1", 0, sink)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    telemetry.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
    telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "otlp")
    yield sink
    server.shutdown()
    thread.join()


def test_metrics_reach_a_collector_with_this_processs_identity(collector: _Sink) -> None:
    _ACCEPTED.add(3)
    METRICS.close()  # flushes, as a worker's exit does

    metrics = ExportMetricsServiceRequest.FromString(collector.bodies["/v1/metrics"]).resource_metrics[0]
    resource = {a.key: a.value.string_value for a in metrics.resource.attributes}
    (counter,) = (m for scope in metrics.scope_metrics for m in scope.metrics if m.name == "test.accepted")
    assert counter.sum.data_points[0].as_int == 3
    assert resource["service.name"] == "dlboard"
    assert resource["service.instance.id"]
