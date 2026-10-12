"""
End-to-end: a Prometheus scrape of a real server that a training run has just logged to.

Spans the request hooks, the REST backend and the SQL store.
"""

from __future__ import annotations

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
def test_a_scrape_reports_what_a_run_just_did(backend_server: BackendServer) -> None:
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
    api.finish_run(run.id, models.RunStatus.FINISHED)

    scrape = _scrape(backend_server)
    assert scraped_total(scrape, "dlboard_ingest_metric_values_total") == 2
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
