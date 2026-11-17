"""
An upload's trace across the two hops no library instruments: the write queue into the spawned writer, and the
writer's return queue into the thread that records refs.

The writer is a real spawned process, so it exports to a real (in-process) OTLP collector; spans cannot be collected
in memory across a process boundary.
"""

from __future__ import annotations

import io
import time
from typing import TYPE_CHECKING, Any

from werkzeug.datastructures import FileStorage

from dlboard import models
from dlboard._telemetry.traces import SpanKind, flush, span, trace_ids
from dlboard.plugins.data_stores.filesystem import FSBlobs
from dlboard.plugins.data_stores.tests import conftest as uploads

if TYPE_CHECKING:
    from pathlib import Path

    from dlboard.conftest import MakeBlobStore, OtlpTraces


def test_an_uploads_trace_follows_it_through_the_writer_and_into_the_recording_of_its_ref(
    otlp_traces: OtlpTraces, tmp_path: Path, make_blob_store: MakeBlobStore
) -> None:
    store = make_blob_store(FSBlobs(tmp_path), 10)
    data_store = _RecordingStore()
    uploads.ingest(store, data_store)
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

    by_name = {s.name: s for s in otlp_traces.spans}
    stage, write, ingest = (by_name[f"dlboard.blob.{hop}"] for hop in ("stage", "write", "ingest"))
    assert stage.parent_span_id.hex() == write.parent_span_id.hex() == request_span
    assert write.trace_id.hex() == request_trace
    assert [link.span_id for link in ingest.links] == [write.span_id]
    assert {("dlboard.experiment.id", 2), ("dlboard.run.id", 1)} <= {
        (a.key, a.value.int_value) for a in write.attributes
    }
    # How long each queue held it, on the span that took it off: a trace shows it without following the link.
    for consumer in (write, ingest):
        (wait,) = (a.value.double_value for a in consumer.attributes if a.key == "dlboard.queue.wait")
        assert 0 <= wait < 10


class _RecordingStore:
    def __init__(self) -> None:
        self.recorded: list[models.Artifact] = []

    def log_artifact_refs(self, artifacts: list[models.Artifact]) -> None:
        self.recorded.extend(artifacts)

    def fetch_artifacts(self, *_args: Any, **_kwargs: Any) -> list[models.Artifact]:  # noqa: ANN401
        return self.recorded
