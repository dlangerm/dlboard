# pyright: reportPrivateUsage=false
"""End-to-end: a real `DLTrackLogger` (real shipping processes) logging to a live dltrack backend."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
import requests
import torch

from dltrack import models
from dltrack.client import DLTrackLogger
from dltrack.plugins.artifacts import image
from dltrack.plugins.backend.basic_rest_backend import create_path

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dltrack.conftest import BackendServer

# Spawning the shipping processes re-imports torch/lightning in each, routinely past the slow-startup
# warning's threshold on a test box -- expected here, not a finding.
pytestmark = pytest.mark.filterwarnings("ignore:dltrack's logging worker processes took")


@pytest.fixture
def logger(backend_server: BackendServer) -> Iterator[DLTrackLogger]:
    logger = DLTrackLogger.from_names("shipping", server_url=backend_server.url)
    yield logger
    for shipper in (logger._metrics, logger._artifacts):
        shipper.process.kill()


def _image(step: int) -> image.Image:
    return image.Image(key="img", image=torch.zeros(4, 4, dtype=torch.uint8), step=step)


def test_finalize_ships_everything_logged_before_it(
    logger: DLTrackLogger, backend_server: BackendServer
) -> None:
    """Lightning calls `finalize` right before a script usually exits, killing the daemon shippers."""
    for step in range(3):
        logger.log_metrics({"loss": float(step)}, step=step)
    logger.log_artifact([_image(0)])

    logger.finalize("success")

    assert list(backend_server.store.fetch_metrics(logger._experiment_id).step) == [0, 1, 2]
    # The server itself records an uploaded artifact's ref asynchronously (see `FSArtifactStore`),
    # a second or so after accepting the upload -- `finalize`'s guarantee ends at "accepted".
    deadline = time.monotonic() + 10
    while not list(backend_server.store.fetch_artifacts(experiment_id=logger._experiment_id)):
        assert time.monotonic() < deadline, "the uploaded artifact never showed up"
        time.sleep(0.1)


def test_a_metric_without_a_step_is_rejected_where_it_was_logged(logger: DLTrackLogger) -> None:
    with pytest.raises(ValueError, match="explicit step"):
        logger.log_metrics({"loss": 1.0})


def test_logging_an_artifact_after_its_shipping_process_died_raises(logger: DLTrackLogger) -> None:
    """`log_artifact` used to check the *metrics* process instead, queueing into the void."""
    logger._artifacts.process.kill()
    logger._artifacts.process.join()

    with pytest.raises(RuntimeError, match="artifacts shipping process died"):
        logger.log_artifact([_image(0)])


def test_an_invalid_metric_batch_is_a_400_not_a_500(backend_server: BackendServer) -> None:
    """The client's shipping loop drops a 4xx batch but retries a 5xx one, so the difference matters."""
    res = requests.post(
        create_path(models.LoggedMetrics, backend_server.url), json=[{"metrics": {}}], timeout=10
    )

    assert res.status_code == 400
