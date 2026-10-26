"""
End-to-end: a Prometheus scrape of a real server that a training run has just logged to.

Spans the request hooks, the REST backend, the SQL store, and the blob store's *spawned* writer
process -- whose numbers can only reach `/metrics` through the shared snapshot directory.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pendulum
import pytest
import requests

from dlboard import models
from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.conftest import scraped_total

if TYPE_CHECKING:
    from pathlib import Path

    from dlboard.conftest import BackendServer

_SCRAPE_TIMEOUT_S = 15


@pytest.fixture
def prometheus(telemetry: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Switched on *before* `backend_server` builds its app (fixtures are created in argument order)."""
    (tmp_path / "metrics").mkdir()
    telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "prometheus")
    telemetry.setenv("DLBOARD_TELEMETRY_PROMETHEUS_DIR", str(tmp_path / "metrics"))
    telemetry.setenv("DLBOARD_TELEMETRY_PROMETHEUS_SNAPSHOT_S", "0.1")


def _scrape(server: BackendServer) -> str:
    return requests.get(f"{server.url}/metrics", timeout=5).text


@pytest.mark.usefixtures("prometheus")
def test_a_scrape_reports_what_a_run_just_did_including_the_artifact_writer_process(
    backend_server: BackendServer, tmp_path: Path
) -> None:
    api = BasicDlboardAPI(backend_server.url)
    experiment = api.get_or_create_experiment(api.get_or_create_project("p").id, "e")
    run = api.create_run(models.NewRun(experiment_id=experiment.id))
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                metrics={"loss": 1.0, "acc": 0.5},
                step=0,
                experiment_id=experiment.id,
                run_id=run.id,
                timestamp_utc=pendulum.now(pendulum.UTC),
            )
        ]
    )
    blob = tmp_path / "x.bin"
    blob.write_bytes(b"x" * 64)
    artifact = models.NewArtifact(
        key="k", fname=blob.name, run_id=run.id, experiment_id=experiment.id, step=0
    )
    api.log_artifact_batch([(artifact, blob)])
    api.finish_run(run.id, models.RunStatus.FINISHED)

    # The writer is another process that reports on its own timer, and the ref is recorded after it, so wait for both.
    finished = (
        "dlboard_artifact_blob_write_duration_seconds_count",
        "dlboard_artifact_ingest_duration_seconds_count",
    )
    deadline = time.monotonic() + _SCRAPE_TIMEOUT_S
    scrape = _scrape(backend_server)
    while not all(scraped_total(scrape, f, dlboard_outcome="success") for f in finished):
        assert time.monotonic() < deadline, f"the upload's numbers never arrived:\n{scrape}"
        time.sleep(0.1)
        scrape = _scrape(backend_server)

    # Every hop of the upload's way, from the request that staged it to the recording of its ref.
    for hop in ("stage_duration", "write_queue_wait", "ingest_queue_wait"):
        assert scraped_total(scrape, f"dlboard_artifact_{hop}_seconds_count") == 1

    assert scraped_total(scrape, "dlboard_ingest_metric_values_total") == 2
    assert scraped_total(scrape, "dlboard_ingest_artifacts_total") == 1
    assert scraped_total(scrape, "dlboard_runs_created_total") == 1
    assert scraped_total(scrape, "dlboard_runs_finished_total", dlboard_run_status="finished") == 1
    assert scraped_total(
        scrape,
        "http_server_request_duration_seconds_count",
        http_request_method="POST",
        http_route="/api/v1/metrics",
        http_response_status_code="200",
    )
    assert scraped_total(scrape, "target_info") == 0  # one process's metadata is not a number to add up
