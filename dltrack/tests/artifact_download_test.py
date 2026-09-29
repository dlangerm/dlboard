"""End-to-end: an uploaded artifact is fetched back by id through `/artifact/<id>`, not by ref."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import requests

from dltrack import models
from dltrack.plugins.backend.basic_rest_backend import BasicDltrackAPI

if TYPE_CHECKING:
    from pathlib import Path

    from dltrack.conftest import BackendServer
    from dltrack.serve import SQLStoreBase

_INGEST_TIMEOUT_S = 10


def _log_one_artifact(api: BasicDltrackAPI, run: models.Run, experiment_id: int, path: Path) -> None:
    path.write_bytes(b"hello")
    api.log_artifact_batch(
        [models.NewArtifact(key="img", fname=path.name, run_id=run.id, experiment_id=experiment_id, step=0)],
        [path],
    )


def _wait_for_artifact(store: SQLStoreBase[Any], experiment_id: int) -> models.Artifact:
    """The server records an uploaded artifact's ref asynchronously (see `BlobArtifactStore`)."""
    deadline = time.monotonic() + _INGEST_TIMEOUT_S
    while not (stored := list(store.fetch_artifacts(experiment_id=experiment_id))):
        assert time.monotonic() < deadline, "artifact was never recorded"
        time.sleep(0.1)
    return stored[0]


def test_an_uploaded_artifacts_bytes_are_served_by_id(backend_server: BackendServer, tmp_path: Path) -> None:
    api = BasicDltrackAPI(backend_server.url)
    experiment = api.get_or_create_experiment(api.get_or_create_project("p").id, "e")
    run = api.create_run(models.NewRun(experiment_id=experiment.id))
    _log_one_artifact(api, run, experiment.id, tmp_path / "a.bin")
    artifact = _wait_for_artifact(backend_server.store, experiment.id)

    response = requests.get(f"{backend_server.url}/artifact/{artifact.id}", timeout=5)

    assert response.status_code == 200
    assert response.content == b"hello"


def test_an_unknown_artifact_id_404s(backend_server: BackendServer) -> None:
    response = requests.get(f"{backend_server.url}/artifact/999999", timeout=5)

    assert response.status_code == 404


def test_a_soft_deleted_artifacts_id_404s(backend_server: BackendServer, tmp_path: Path) -> None:
    api = BasicDltrackAPI(backend_server.url)
    experiment = api.get_or_create_experiment(api.get_or_create_project("p").id, "e")
    run = api.create_run(models.NewRun(experiment_id=experiment.id))
    _log_one_artifact(api, run, experiment.id, tmp_path / "a.bin")
    artifact = _wait_for_artifact(backend_server.store, experiment.id)
    admin = backend_server.store.get_or_create_user("admin")
    assert artifact.id is not None
    backend_server.store.delete_artifact(artifact.id, admin)

    response = requests.get(f"{backend_server.url}/artifact/{artifact.id}", timeout=5)

    assert response.status_code == 404
