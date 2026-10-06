# pyright: reportPrivateUsage=false
"""End-to-end: a real `DLBoardLogger` (real shipping processes) logging to a live dlboard backend."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
import requests
import torch

from dlboard._wire import Resource
from dlboard.client._rest_api import create_path
from dlboard.client.artifacts import image
from dlboard.client.dlboard_logger import DLBoardLogger
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dlboard.conftest import BackendServer

# Spawning the shipping processes re-imports torch/lightning in each, routinely past the slow-startup
# warning's threshold on a test box -- expected here, not a finding.
pytestmark = pytest.mark.filterwarnings("ignore:dlboard's logging worker processes took")


@pytest.fixture
def logger(backend_server: BackendServer) -> Iterator[DLBoardLogger]:
    logger = DLBoardLogger.from_names("shipping", server_url=backend_server.url)
    yield logger
    for shipper in (logger._metrics, logger._artifacts):
        shipper.process.kill()


def _image(step: int) -> image.Image:
    return image.Image(key="img", image=torch.zeros(4, 4, dtype=torch.uint8), step=step)


@pytest.mark.parametrize("store_backend", EVERY_STORE_BACKEND)
def test_finalize_ships_everything_logged_before_it(
    logger: DLBoardLogger, backend_server: BackendServer, store_backend: StoreBackend
) -> None:
    """
    Lightning calls `finalize` right before a script usually exits, killing the daemon shippers.

    The whole path -- client shipping processes, REST ingest, store -- on every `store_backend`.
    """
    del store_backend  # only selects which database `backend_server` runs on
    for step in range(3):
        logger.log_metrics({"loss": float(step)}, step=step)
    logger.log_artifact([_image(0)])

    logger.finalize("success")

    assert list(backend_server.store.fetch_metrics(logger._experiment_id).step) == [0, 1, 2]
    # The server itself records an uploaded artifact's ref asynchronously (see `BlobArtifactStore`),
    # a second or so after accepting the upload -- `finalize`'s guarantee ends at "accepted".
    deadline = time.monotonic() + 10
    while not list(backend_server.store.fetch_artifacts(experiment_id=logger._experiment_id)):
        assert time.monotonic() < deadline, "the uploaded artifact never showed up"
        time.sleep(0.1)


def test_a_second_logger_given_the_run_id_logs_into_the_same_run(
    logger: DLBoardLogger, backend_server: BackendServer
) -> None:
    """The multi-process case: a separately launched process attaches instead of creating its own run."""
    second = DLBoardLogger.from_names("shipping", server_url=backend_server.url, run_id=logger.run_id)
    try:
        second.log_metrics({"loss": 1.0}, step=0)
        second.finalize("success")
    finally:
        for shipper in (second._metrics, second._artifacts):
            shipper.process.kill()

    assert second.run_id == logger.run_id
    assert [run.id for run in backend_server.store.get_runs(logger._experiment_id)] == [logger.run_id]
    assert list(backend_server.store.fetch_metrics(logger._experiment_id).step) == [0]


def test_a_metric_without_a_step_is_rejected_where_it_was_logged(logger: DLBoardLogger) -> None:
    with pytest.raises(ValueError, match="explicit step"):
        logger.log_metrics({"loss": 1.0})


def test_logging_an_artifact_after_its_shipping_process_died_raises(logger: DLBoardLogger) -> None:
    """`log_artifact` used to check the *metrics* process instead, queueing into the void."""
    logger._artifacts.process.kill()
    logger._artifacts.process.join()

    with pytest.raises(RuntimeError, match="artifacts shipping process died"):
        logger.log_artifact([_image(0)])


def test_an_invalid_metric_batch_is_a_400_not_a_500(backend_server: BackendServer) -> None:
    """The client's shipping loop drops a 4xx batch but retries a 5xx one, so the difference matters."""
    res = requests.post(create_path(Resource.METRICS, backend_server.url), json=[{"metrics": {}}], timeout=10)

    assert res.status_code == 400
