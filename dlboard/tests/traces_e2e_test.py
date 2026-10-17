"""
End to end: one shipped batch is one trace, from the client's shipping process to the database and the blob writer.

A real `DLBoardLogger` (real spawned shipping processes) logs to a live backend (a real spawned blob writer), and
every process exports to the same OTLP collector. What each hop adds is tested where it is made; this proves they
join up -- which only a test spanning all of them can.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import TYPE_CHECKING

import pytest
import torch
from opentelemetry.proto.trace.v1.trace_pb2 import Span

from dlboard.client.artifacts import image
from dlboard.client.dlboard_logger import DLBoardLogger
from dlboard.conftest import EVERY_ARTIFACT_BACKEND, ArtifactBackend
from dlboard.tests.logger_shipping_test import _abandon

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dlboard.conftest import BackendServer, OtlpTraces

pytestmark = pytest.mark.filterwarnings("ignore:dlboard's logging worker processes took")

_METRICS_REQUEST = "POST /api/v1/metrics"
_ARTIFACTS_REQUEST = "POST /api/v1/artifacts"
_WAIT_SEC = 30


@pytest.fixture
def logger(otlp_traces: OtlpTraces, backend_server: BackendServer) -> Iterator[DLBoardLogger]:
    del otlp_traces  # only so tracing is on, and exporting somewhere, before the server and the logger start
    logger = DLBoardLogger.from_names("traced", server_url=backend_server.url)
    yield logger
    _abandon(logger)


def _everything_arrived(spans: list[Span]) -> bool:
    names = Counter(s.name for s in spans)
    return (
        names["dlboard.ship"] == 2
        and names[_METRICS_REQUEST] == 2  # the client's span and the server's
        and names[_ARTIFACTS_REQUEST] == 2
        and names["dlboard.blob.write"] == 1
        and names["dlboard.blob.ingest"] == 1
    )


def _is_sql(span: Span) -> bool:
    return any(attribute.key == "db.system" for attribute in span.attributes)


@pytest.mark.parametrize("artifact_backend", EVERY_ARTIFACT_BACKEND)
def test_a_shipped_batch_is_one_trace_from_the_client_to_the_database_and_the_blob_writer(
    logger: DLBoardLogger, otlp_traces: OtlpTraces, artifact_backend: ArtifactBackend
) -> None:
    logger.log_metrics({"loss": 1.0}, step=0)
    logger.log_artifact([image.Image(key="img", image=torch.zeros(4, 4, dtype=torch.uint8), step=0)])
    logger.finalize("success")

    deadline = time.monotonic() + _WAIT_SEC
    while not _everything_arrived(otlp_traces.spans):
        names = sorted(Counter(s.name for s in otlp_traces.spans).items())
        assert time.monotonic() < deadline, (
            f"timed out; saw {names} {[(s.name, s.kind) for s in otlp_traces.spans if 'api' in s.name]}"
        )
        time.sleep(0.1)
    spans = list(otlp_traces.spans)

    for request in (_METRICS_REQUEST, _ARTIFACTS_REQUEST):
        client, server = sorted((s for s in spans if s.name == request), key=lambda s: -s.kind)
        assert (client.kind, server.kind) == (Span.SPAN_KIND_CLIENT, Span.SPAN_KIND_SERVER)
        (ship,) = (s for s in spans if s.name == "dlboard.ship" and s.trace_id == client.trace_id)
        assert client.parent_span_id == ship.span_id, "the shipper's batch is the request's parent"
        assert server.trace_id == client.trace_id, "the server continues the client's trace"
        assert server.parent_span_id == client.span_id
        assert any(_is_sql(s) and s.trace_id == client.trace_id for s in spans), "the SQL it ran is traced"

    (write,) = (s for s in spans if s.name == "dlboard.blob.write")
    (ingest,) = (s for s in spans if s.name == "dlboard.blob.ingest")
    (artifact_request,) = (
        s for s in spans if s.name == _ARTIFACTS_REQUEST and s.kind == Span.SPAN_KIND_SERVER
    )
    assert write.trace_id == artifact_request.trace_id, (
        "the writer continues the upload's trace across the queue"
    )
    assert [link.span_id for link in ingest.links] == [write.span_id]
    if artifact_backend is ArtifactBackend.S3:
        # Not asserted to nest under the write: boto uploads from its own threads, which don't inherit the trace, so
        # `S3.PutObject` is a trace of its own. Tracing doesn't change how the upload runs.
        assert any(s.name == "S3.PutObject" for s in spans), "boto's calls to S3 are traced"
