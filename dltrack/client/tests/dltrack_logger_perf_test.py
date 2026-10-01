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
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import torch

from dltrack import models
from dltrack.plugins.artifacts import image
from dltrack.plugins.backend.basic_rest_backend import BasicDltrackAPI

if TYPE_CHECKING:
    from pathlib import Path

    from dltrack.conftest import BackendServer

_MOSAIC_COUNT = 8
_MOSAIC_SIDE_PX = 512
_UPLOAD_BUDGET_SEC = 5.0
"""Generous enough not to flake on a loaded CI box; tight enough that a real per-request or
per-image regression still trips it -- colocated has no real network hop to blame slack on."""


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
    backend_server: BackendServer, tmp_path: Path
) -> None:
    api = BasicDltrackAPI(base_url=backend_server.url)
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

    start = time.perf_counter()
    api.log_artifact_batch(pairs)
    elapsed = time.perf_counter() - start

    assert elapsed < _UPLOAD_BUDGET_SEC, (
        f"Logging {_MOSAIC_COUNT} {_MOSAIC_SIDE_PX}x{_MOSAIC_SIDE_PX} images to a colocated server "
        f"took {elapsed:.2f}s, over the {_UPLOAD_BUDGET_SEC}s budget -- with no real network hop in "
        "the way, this points at dltrack's own overhead, not network latency."
    )
