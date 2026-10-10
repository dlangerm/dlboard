"""
End-to-end: an uploaded artifact is fetched back by id through `/artifact/<id>`, not by ref.

Parametrized over every `artifact_backend` -- a chart/client only ever fetches by id, so this
route's behavior must be identical whichever `BlobBackend` is doing the actual serving.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest
import requests

from dlboard import models
from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.conftest import EVERY_ARTIFACT_BACKEND, ArtifactBackend
from dlboard.plugins.data_stores.s3 import S3DownloadMode
from dlboard.serve._backend._artifact_download import _IMMUTABLE_CACHE_CONTROL, artifact_version

if TYPE_CHECKING:
    from pathlib import Path

    from dlboard.conftest import BackendServer
    from dlboard.serve import SQLStoreBase

# The S3 backend's first write of a session pays for a cold boto3 session plus a real network
# round trip to the (containerized) store, on top of the usual async-ingest delay -- 30s matches
# the headroom `docs_screenshots_test.py` already gives the same wait on a loaded CI runner.
_INGEST_TIMEOUT_S = 30


@pytest.fixture(params=EVERY_ARTIFACT_BACKEND)
def artifact_backend(request: pytest.FixtureRequest) -> ArtifactBackend:
    """Override the default (filesystem-only) fixture: every test below runs on every backend."""
    return request.param


def _log_one_artifact(api: BasicDlboardAPI, run: models.Run, experiment_id: int, path: Path) -> None:
    path.write_bytes(b"hello")
    new_artifact = models.NewArtifact(
        key="img", fname=path.name, run_id=run.id, experiment_id=experiment_id, step=0
    )
    api.log_artifact_batch([(new_artifact, path)])


def _wait_for_artifact(store: SQLStoreBase[Any], experiment_id: int) -> models.Artifact:
    """The server records an uploaded artifact's ref asynchronously (see `BlobArtifactStore`)."""
    deadline = time.monotonic() + _INGEST_TIMEOUT_S
    while not (stored := list(store.fetch_artifacts(experiment_id=experiment_id))):
        assert time.monotonic() < deadline, "artifact was never recorded"
        time.sleep(0.1)
    return stored[0]


def test_an_uploaded_artifacts_bytes_are_served_by_id(backend_server: BackendServer, tmp_path: Path) -> None:
    api = BasicDlboardAPI(backend_server.url)
    experiment = api.get_or_create_experiment(api.get_or_create_project("p").id, "e")
    run = api.create_run(models.NewRun(experiment_id=experiment.id))
    _log_one_artifact(api, run, experiment.id, tmp_path / "a.bin")
    artifact = _wait_for_artifact(backend_server.store, experiment.id)

    # The versioned URL the charts use, not just the bare id: the route has to ignore the `v` token.
    response = requests.get(
        f"{backend_server.url}/artifact/{artifact.id}?v={artifact_version(artifact)}", timeout=5
    )

    assert response.status_code == 200
    assert response.content == b"hello"


@pytest.mark.s3
@pytest.mark.parametrize("artifact_backend", [ArtifactBackend.S3], indirect=True)
@pytest.mark.parametrize("s3_download_mode", [S3DownloadMode.PRESIGN], indirect=True)
def test_a_presigned_downloads_redirect_is_never_cached_as_long_as_the_bytes_it_points_to(
    backend_server: BackendServer, tmp_path: Path, s3_download_mode: S3DownloadMode
) -> None:
    """
    `S3Blobs.download` in presign mode 302s to a URL that expires -- the redirect itself must not
    get the artifact's own, year-long `Cache-Control`, or a browser would replay it past expiry.
    """
    del s3_download_mode  # only selects `backend_server`'s S3 download mode, via indirect parametrize
    api = BasicDlboardAPI(backend_server.url)
    experiment = api.get_or_create_experiment(api.get_or_create_project("p").id, "e")
    run = api.create_run(models.NewRun(experiment_id=experiment.id))
    _log_one_artifact(api, run, experiment.id, tmp_path / "a.bin")
    artifact = _wait_for_artifact(backend_server.store, experiment.id)

    response = requests.get(f"{backend_server.url}/artifact/{artifact.id}", timeout=5)

    assert response.status_code == 200
    assert response.content == b"hello"
    assert len(response.history) == 1, "expected exactly one presigned redirect hop"
    assert response.history[0].headers.get("Cache-Control") != _IMMUTABLE_CACHE_CONTROL


def test_an_unknown_artifact_id_404s(backend_server: BackendServer) -> None:
    response = requests.get(f"{backend_server.url}/artifact/999999", timeout=5)

    assert response.status_code == 404


def test_a_soft_deleted_artifacts_id_404s(backend_server: BackendServer, tmp_path: Path) -> None:
    api = BasicDlboardAPI(backend_server.url)
    experiment = api.get_or_create_experiment(api.get_or_create_project("p").id, "e")
    run = api.create_run(models.NewRun(experiment_id=experiment.id))
    _log_one_artifact(api, run, experiment.id, tmp_path / "a.bin")
    artifact = _wait_for_artifact(backend_server.store, experiment.id)
    admin = backend_server.store.get_or_create_user(models.Principal.unverified("admin"))
    assert artifact.id is not None
    backend_server.store.delete_artifact(artifact.id, admin)

    response = requests.get(f"{backend_server.url}/artifact/{artifact.id}", timeout=5)

    assert response.status_code == 404
