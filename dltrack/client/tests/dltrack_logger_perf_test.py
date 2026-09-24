"""
Performance regression coverage for image logging: a real server and a real HTTP client, colocated.

Colocated (client and server on the same machine, which is how most dltrack deployments run) is the
case that has to always be fast -- there's no network hop in the way, so any slowness here is
dltrack's own overhead (image encoding, HTTP handling, sqlite/filesystem writes), not something a
user could instead blame on their network. This logs a batch of realistically large images (roughly
`train.py`'s startup mosaic) through a real HTTP round-trip on localhost and asserts it stays inside
a generous-but-not-toothless budget -- loose enough not to flake on a slow CI box, tight enough to
still catch a real regression (e.g. per-request overhead creeping back in, or a batched upload
silently becoming one request per file).

Deliberately builds its own app from just the storage/backend/auth plugins (`LOCAL_STORAGE` +
`BUILTIN_BACKEND` + `LOCAL_AUTH`), not the full `LOCAL_DEPLOYMENT` bundle `browser_test.py` uses --
`BUILTIN_CHARTS` registers chart types into a process-global registry that rejects a second
registration, so only one test process-wide gets to build an app with those. Skipping it here (this
test never touches a chart) means this file can build its own app safely no matter what else is in
the same pytest run. Page layouts (home/project/admin/experiment) are no longer an opt-in bundle --
`dltrack.serve.app.app()` always wires them in, and re-registering the same page paths across
multiple `Dash` instances in one process is harmless (confirmed by running this file alongside
`browser_test.py`'s full-bundle app in the same pytest session).
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import pytest
import torch
from werkzeug.serving import make_server

from dltrack import models
from dltrack.plugins import BUILTIN_BACKEND, LOCAL_AUTH, LOCAL_STORAGE
from dltrack.plugins.artifacts import image
from dltrack.plugins.backend.basic_rest_backend import BasicDltrackAPI
from dltrack.serve import app as build_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_MOSAIC_COUNT = 8
_MOSAIC_SIDE_PX = 512
_UPLOAD_BUDGET_SEC = 5.0
"""Generous enough not to flake on a loaded CI box; tight enough that a real per-request or
per-image regression still trips it -- colocated has no real network hop to blame slack on."""


@pytest.fixture
def live_server_url(tmp_path: Path) -> Iterator[str]:
    """A real dltrack backend (storage + REST routes, no charts/pages) on a background thread."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("SQLITE_LOCATION", str(tmp_path / "test.sqlite"))
    monkeypatch.setenv("ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    try:
        app = build_app([*LOCAL_STORAGE, *LOCAL_AUTH, *BUILTIN_BACKEND])
    finally:
        monkeypatch.undo()
    server = make_server("127.0.0.1", 0, app.server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


def _mosaic_batch(count: int) -> list[image.Image]:
    """`count` large single-channel images -- big enough to make a real upload cost, not a network mock."""
    return [
        image.Image(
            key="perf/mosaic",
            image=(torch.rand(_MOSAIC_SIDE_PX, _MOSAIC_SIDE_PX) * 255).to(torch.uint8),
            step=i,
        )
        for i in range(count)
    ]


def test_logging_a_batch_of_large_images_stays_within_budget_when_colocated(
    live_server_url: str, tmp_path: Path
) -> None:
    api = BasicDltrackAPI(base_url=live_server_url)
    project = api.get_or_create_project("perf")
    experiment = api.create_experiment(
        models.NewExperiment(project_id=project.id, source=models.ExperimentSource.PYTORCH_LIGHTNING)
    )
    run = api.create_run(models.NewRun(experiment_id=experiment.id))

    local_temp = tmp_path / "staged"
    local_temp.mkdir()
    pairs = [
        img.to_artifact(local_temp, run_id=run.id, experiment_id=experiment.id)
        for img in _mosaic_batch(_MOSAIC_COUNT)
    ]
    artifacts = [obj for obj, _ in pairs]
    files = [path for _, path in pairs]

    start = time.perf_counter()
    api.log_artifact_batch(artifacts, files)
    elapsed = time.perf_counter() - start

    assert elapsed < _UPLOAD_BUDGET_SEC, (
        f"Logging {_MOSAIC_COUNT} {_MOSAIC_SIDE_PX}x{_MOSAIC_SIDE_PX} images to a colocated server "
        f"took {elapsed:.2f}s, over the {_UPLOAD_BUDGET_SEC}s budget -- with no real network hop in "
        "the way, this points at dltrack's own overhead, not network latency."
    )
