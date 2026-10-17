"""
An upload's trace across the two hops no library instruments: the write queue into the spawned writer, and the
writer's return queue into the thread that records refs.

The writer is a real spawned process, so it exports to a real (in-process) OTLP collector; spans cannot be collected
in memory across a process boundary.
"""

from __future__ import annotations

import io
import threading
import time
from typing import TYPE_CHECKING

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from werkzeug.datastructures import FileStorage
from werkzeug.serving import make_server

from dlboard import models
from dlboard._telemetry.traces import SpanKind, flush, span, trace_ids
from dlboard.plugins.data_stores.filesystem import FSBlobs
from dlboard.plugins.data_stores.tests.blob_store_test import _FakeApp
from dlboard.serve import set_data_store

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from wsgiref.types import InputStream, StartResponse, WSGIEnvironment

    from opentelemetry.proto.trace.v1.trace_pb2 import Span

    from dlboard.conftest import MakeBlobStore


class _Collector:
    """Keeps every span it is sent, from every process."""

    def __init__(self) -> None:
        self.spans: list[Span] = []

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> list[bytes]:
        stream: InputStream = environ["wsgi.input"]
        body = stream.read(int(environ["CONTENT_LENGTH"]))
        for resource_spans in ExportTraceServiceRequest.FromString(body).resource_spans:
            for scope_spans in resource_spans.scope_spans:
                self.spans.extend(scope_spans.spans)
        start_response("200 OK", [("Content-Type", "application/x-protobuf")])
        return [b""]


@pytest.fixture
def collector(telemetry: pytest.MonkeyPatch) -> Iterator[_Collector]:
    sink = _Collector()
    server = make_server("127.0.0.1", 0, sink)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    telemetry.setenv("DLBOARD_TELEMETRY_TRACES", "true")  # inherited by the spawned writer
    telemetry.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
    yield sink
    server.shutdown()
    thread.join()


def test_an_uploads_trace_follows_it_through_the_writer_and_into_the_recording_of_its_ref(
    collector: _Collector, tmp_path: Path, make_blob_store: MakeBlobStore
) -> None:
    store = make_blob_store(FSBlobs(tmp_path), 10)
    data_store = _RecordingStore()
    app = _FakeApp()
    set_data_store(app, data_store)  # pyrefly: ignore [bad-argument-type]
    threading.Thread(target=store.ingest_stored_artifacts, args=(app,), daemon=True).start()
    upload = models.NewArtifact(key="img", fname="a.png", run_id=1, experiment_id=2, step=0)

    with span("request", SpanKind.SERVER):
        store.log_artifacts([(upload, FileStorage(io.BytesIO(b"x"), filename="a.png"))])
        ids = trace_ids()
    assert ids is not None
    request_trace, request_span = ids
    deadline = time.monotonic() + 10
    while not data_store.recorded and time.monotonic() < deadline:
        time.sleep(0.05)
    store.close()  # the writer exports what it has before it stops
    flush()
    time.sleep(0.5)  # the ingest span ends just after the ref is recorded

    by_name = {s.name: s for s in collector.spans}
    write, ingest = by_name["dlboard.blob.write"], by_name["dlboard.blob.ingest"]
    assert write.trace_id.hex() == request_trace
    assert write.parent_span_id.hex() == request_span
    assert [link.span_id for link in ingest.links] == [write.span_id]
    assert [(a.key, a.value.int_value) for a in write.attributes] == [
        ("dlboard.experiment.id", 2),
        ("dlboard.run.id", 1),
    ]


class _RecordingStore:
    def __init__(self) -> None:
        self.recorded: list[models.Artifact] = []

    def log_artifact_refs(self, artifacts: list[models.Artifact]) -> None:
        self.recorded.extend(artifacts)
