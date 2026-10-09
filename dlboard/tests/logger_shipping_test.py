# pyright: reportPrivateUsage=false
"""End-to-end: a real `DLBoardLogger` (real shipping processes) logging to a live dlboard backend."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Literal

import pytest
import pytorch_lightning as pl
import requests
import torch
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.demos.boring_classes import BoringModel

from dlboard._wire import Resource, run_finish_path
from dlboard.client._rest_api import create_path
from dlboard.client.artifacts import image
from dlboard.client.dlboard_logger import DLBoardLogger
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend
from dlboard.models import FILE_KIND_TAG, FileKind, RunStatus

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

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


@pytest.mark.parametrize(
    ("log_model", "expected_keys"),
    [
        pytest.param(
            "all",
            {f"checkpoints/epoch={e}-step={2 * (e + 1)}.ckpt" for e in range(3)} | {"checkpoints/last.ckpt"},
            id="every-save-even-ones-rotated-away-before-shipping",
        ),
        pytest.param(
            True,
            {"checkpoints/epoch=2-step=6.ckpt", "checkpoints/last.ckpt"},
            id="only-the-kept-ones-at-finalize",
        ),
    ],
)
@pytest.mark.filterwarnings(
    "ignore::lightning_fabric.utilities.warnings.PossibleUserWarning"
)  # GPU/worker-count advice
@pytest.mark.filterwarnings(
    "ignore:.isinstance\\(treespec, LeafSpec\\).:FutureWarning"
)  # torch/Lightning internals
def test_log_model_uploads_the_checkpoints_a_real_fit_saves(
    backend_server: BackendServer, tmp_path: Path, log_model: bool | Literal["all"], expected_keys: set[str]
) -> None:
    logger = DLBoardLogger.from_names("shipping", server_url=backend_server.url, log_model=log_model)
    trainer = pl.Trainer(
        max_epochs=3,
        limit_train_batches=2,
        limit_val_batches=0,
        accelerator="cpu",
        logger=logger,
        callbacks=[ModelCheckpoint(dirpath=tmp_path / "checkpoints", save_top_k=1, save_last=True)],
        enable_progress_bar=False,
        enable_model_summary=False,
    )
    try:
        trainer.fit(BoringModel())
    finally:
        for shipper in (logger._metrics, logger._artifacts):
            shipper.process.kill()

    assert not logger._unflushed, "the kept checkpoints were queued after the last flush"

    deadline = time.monotonic() + 10
    while True:
        artifacts = list(backend_server.store.fetch_artifacts(experiment_id=logger._experiment_id))
        if {a.key for a in artifacts} >= expected_keys or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert {a.key for a in artifacts} == expected_keys
    assert {a.tags[FILE_KIND_TAG] for a in artifacts} == {FileKind.CHECKPOINT.value}
    assert all(a.fname.endswith(".ckpt") for a in artifacts)


def test_finalize_records_how_the_run_ended_and_the_newest_report_wins(
    logger: DLBoardLogger, backend_server: BackendServer
) -> None:
    """The whole path: the client's `finalize`, the REST route, the store. Lightning finalizes per stage."""
    run = backend_server.store.get_run(logger.run_id or 0)
    assert run is not None
    assert (run.status, run.ended_at) == (RunStatus.RUNNING, None)

    logger.finalize("success")
    finished = backend_server.store.get_run(run.id)
    assert finished is not None
    assert (finished.status, finished.ended_at is not None) == (RunStatus.FINISHED, True)

    logger.finalize("failed")
    failed = backend_server.store.get_run(run.id)
    assert failed is not None
    assert failed.status == RunStatus.FAILED


def test_the_exit_flush_does_not_claim_the_run_ended(
    logger: DLBoardLogger, backend_server: BackendServer
) -> None:
    """It can't tell a crash from a script that simply ended, so the run is left as it was."""
    logger.finalize("atexit")

    run = backend_server.store.get_run(logger.run_id or 0)
    assert run is not None
    assert (run.status, run.ended_at) == (RunStatus.RUNNING, None)


@pytest.mark.parametrize(
    ("run_id", "body", "expected_status"),
    [
        pytest.param(None, {"status": "running"}, 400, id="running-is-not-a-way-to-end"),
        pytest.param(None, {"status": "exploded"}, 400, id="unknown-status"),
        pytest.param(999_999, {"status": "finished"}, 404, id="no-such-run"),
    ],
)
def test_the_finish_route_rejects_what_it_cannot_record(
    logger: DLBoardLogger,
    backend_server: BackendServer,
    run_id: int | None,
    body: dict[str, str],
    expected_status: int,
) -> None:
    res = requests.post(
        f"{backend_server.url}/{run_finish_path(str(run_id or logger.run_id))}", json=body, timeout=10
    )

    assert res.status_code == expected_status
