"""End-to-end: artifacts logged through the REST client -- uploaded, or linked by ref -- land intact."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
import requests
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
    pairs: list[tuple[models.NewArtifact, Path]] = []
    for step, blob in blobs.items():
        path = tmp_path / f"{step}.bin"
        path.write_bytes(blob)
        pairs.append(
            (
                models.NewArtifact(
                    key="img", fname=path.name, run_id=run.id, experiment_id=experiment.id, step=step
                ),
                path,
            )
        )

    api.log_artifact_batch(pairs)

    # The server records an uploaded artifact's ref asynchronously (see `BlobArtifactStore`).
    deadline = time.monotonic() + _INGEST_TIMEOUT_S
    while len(stored := list(backend_server.store.fetch_artifacts(experiment_id=experiment.id))) < len(blobs):
        assert time.monotonic() < deadline, f"only {len(stored)} of {len(blobs)} artifacts were recorded"
        time.sleep(0.1)
    stored_blobs = {
        artifact.step: (tmp_path / "artifacts" / str(AnyUrl(artifact.ref).path).lstrip("/")).read_bytes()
        for artifact in stored
    }
    assert stored_blobs == blobs


def test_a_link_into_the_stores_own_space_round_trips(backend_server: BackendServer, tmp_path: Path) -> None:
    """A client that already wrote a blob into the artifact store's own space may just link it."""
    api = BasicDltrackAPI(backend_server.url)
    experiment = api.get_or_create_experiment(api.get_or_create_project("p").id, "e")
    run = api.create_run(models.NewRun(experiment_id=experiment.id))
    blob = tmp_path / "artifacts" / "already-uploaded.bin"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"already here")
    ref = AnyUrl(f"file:///{blob.relative_to(tmp_path / 'artifacts')}")

    api.log_artifact_batch(
        [
            (
                models.NewArtifact(
                    key="img", fname=blob.name, run_id=run.id, experiment_id=experiment.id, step=0
                ),
                ref,
            )
        ]
    )

    (stored,) = backend_server.store.fetch_artifacts(experiment_id=experiment.id)
    assert stored.ref == str(ref)


def test_linking_a_ref_outside_the_stores_own_space_is_rejected(
    backend_server: BackendServer, tmp_path: Path
) -> None:
    """A ref the store didn't write and doesn't allowlist must never be servable through it."""
    api = BasicDltrackAPI(backend_server.url)
    experiment = api.get_or_create_experiment(api.get_or_create_project("p").id, "e")
    run = api.create_run(models.NewRun(experiment_id=experiment.id))
    foreign = AnyUrl("file:///etc/passwd")

    with pytest.raises(requests.HTTPError) as exc_info:
        api.log_artifact_batch(
            [
                (
                    models.NewArtifact(
                        key="img", fname="passwd", run_id=run.id, experiment_id=experiment.id, step=0
                    ),
                    foreign,
                )
            ]
        )

    assert exc_info.value.response is not None
    assert exc_info.value.response.status_code == 400
    assert list(backend_server.store.fetch_artifacts(experiment_id=experiment.id)) == []
