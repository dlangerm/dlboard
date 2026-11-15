"""Driving a `BlobArtifactStore` as a server does: uploads in, refs recorded by its ingest thread."""

from __future__ import annotations

import io
import threading
import time
from typing import TYPE_CHECKING

from dash import Dash
from werkzeug.datastructures import FileStorage

from dlboard import models
from dlboard.conftest import scraped_total
from dlboard.serve import set_data_store
from dlboard.serve.telemetry._prometheus import serve_metrics

if TYPE_CHECKING:
    from collections.abc import Callable

    from dlboard.plugins.data_stores._blob_store import BlobArtifactStore


class _FakeApp(Dash):
    """A bare stand-in for `Dash` -- the ingest thread only needs the data store set on it."""

    def __init__(self) -> None:
        # Deliberately skips `Dash.__init__`: nothing here needs a real app, only an identity to hang state on.
        pass


def ingest(store: BlobArtifactStore, data_store: object) -> None:
    """Start recording into `data_store` the refs of what `store` writes."""
    app = _FakeApp()
    set_data_store(app, data_store)  # pyrefly: ignore [bad-argument-type]
    threading.Thread(target=store.ingest_stored_artifacts, args=(app,), daemon=True).start()


def file(run: models.Run, name: str) -> tuple[models.NewArtifact, FileStorage]:
    """An upload of an image `name` to `run`, as a request carries it."""
    new_artifact = models.NewArtifact(
        key="img", fname=name, run_id=run.id, experiment_id=run.experiment_id, step=0
    )
    return new_artifact, FileStorage(io.BytesIO(b"data"), filename=name)


def upload(store: BlobArtifactStore, run: models.Run, name: str) -> None:
    store.log_artifacts([file(run, name)])


def rejections(reason: str) -> float:
    """How many uploads were turned away for `reason`, by the `/metrics` scrape."""
    return scraped_total(
        serve_metrics().get_data(as_text=True),
        "dlboard_artifact_write_queue_rejections_total",
        dlboard_rejection_reason=reason,
    )


def wait_until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.1)
