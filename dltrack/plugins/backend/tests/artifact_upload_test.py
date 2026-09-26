"""End-to-end: a batch of artifacts logged through the REST client is stored intact on the server."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from pydantic import AnyUrl

from dltrack import models
from dltrack.plugins.backend.basic_rest_backend import BasicDltrackAPI

if TYPE_CHECKING:
    from pathlib import Path

    from dltrack.conftest import BackendServer

_INGEST_TIMEOUT_S = 10


def test_a_batch_of_same_key_artifacts_is_stored_with_each_ones_own_bytes(
    backend_server: BackendServer, tmp_path: Path
) -> None:
    """A training run logs one image per step under one key, and they all arrive in one batch."""
    api = BasicDltrackAPI(backend_server.url)
    experiment = api.get_or_create_experiment(api.get_or_create_project("p").id, "e")
    run = api.create_run(models.NewRun(experiment_id=experiment.id))
    blobs = {step: bytes([step + 1]) * 64 for step in range(3)}
    artifacts: list[models.NewArtifact] = []
    files: list[Path] = []
    for step, blob in blobs.items():
        path = tmp_path / f"{step}.bin"
        path.write_bytes(blob)
        artifacts.append(
            models.NewArtifact(
                key="img", fname=path.name, run_id=run.id, experiment_id=experiment.id, step=step
            )
        )
        files.append(path)

    api.log_artifact_batch(artifacts, files)

    # The server records an uploaded artifact's ref asynchronously (see `FSArtifactStore`).
    deadline = time.monotonic() + _INGEST_TIMEOUT_S
    while len(stored := list(backend_server.store.fetch_artifacts(experiment_id=experiment.id))) < len(blobs):
        assert time.monotonic() < deadline, f"only {len(stored)} of {len(blobs)} artifacts were recorded"
        time.sleep(0.1)
    stored_blobs = {
        artifact.step: (tmp_path / "artifacts" / str(AnyUrl(artifact.ref).path).lstrip("/")).read_bytes()
        for artifact in stored
    }
    assert stored_blobs == blobs
