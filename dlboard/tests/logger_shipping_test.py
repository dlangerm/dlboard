"""End-to-end: a real `DLBoardLogger` (real shipping processes) logging to a live dlboard backend."""

from __future__ import annotations

import atexit
import functools
import math
import time
from typing import TYPE_CHECKING, Literal

import pendulum
import pytest
import pytorch_lightning as pl
import requests
import torch
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.demos.boring_classes import BoringModel

from dlboard._batching import Failure
from dlboard._wire import Resource, RunCall, create_path, run_path
from dlboard.client import run
from dlboard.client._rest_api import BasicDlboardAPI, classify_failure
from dlboard.client.artifacts import image
from dlboard.client.dlboard_logger import DLBoardLogger
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend
from dlboard.models import (
    FILE_KIND_TAG,
    FileKind,
    LoggedMetrics,
    NewArtifact,
    NewRun,
    PinColor,
    Principal,
    Quantity,
    RunStatus,
    SizeUnit,
    TagValue,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from dlboard.conftest import BackendServer

# Spawning the shipping processes re-imports torch/lightning in each, routinely past the slow-startup
# warning's threshold on a test box -- expected here, not a finding.
pytestmark = pytest.mark.filterwarnings("ignore:dlboard's logging worker processes took")


@pytest.fixture(autouse=True)
def _no_logger_left_for_interpreter_exit(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Fail a test that leaves its logger's `atexit` flush registered.

    That flush runs after the whole session, when this test's server is long gone: it can only
    fail then, with a connection error nobody is positioned to attribute to a test.
    """
    armed: set[Callable[[], None]] = set()
    register, unregister = atexit.register, atexit.unregister

    def track(func: Callable[[], None]) -> Callable[[], None]:
        if isinstance(func, functools.partial) and func.func is run.call_if_alive:
            armed.add(func)
        return register(func)

    def untrack(func: Callable[[], None]) -> None:
        armed.discard(func)
        unregister(func)

    monkeypatch.setattr(atexit, "register", track)
    monkeypatch.setattr(atexit, "unregister", untrack)
    yield
    assert not armed, "a DLBoardLogger was never `_abandon`ed, so it would flush to a dead server at exit"


def _shipping(logger: DLBoardLogger) -> run._Shipping:
    """The shipping processes of a rank-0 logger's run."""
    shipping = logger._run._shipping
    assert shipping is not None
    return shipping


def _experiment_of(logger: DLBoardLogger) -> int:
    return _shipping(logger).experiment_id


def _abandon(logger: DLBoardLogger) -> None:
    """End a test's logger without flushing: stop its shippers and its (and its run's) exit-time flush."""
    atexit.unregister(logger._exit_hook)
    atexit.unregister(logger._run._exit_hook)
    for shipper in (_shipping(logger).metrics, _shipping(logger).artifacts):
        shipper.worker.process.kill()


@pytest.fixture
def logger(backend_server: BackendServer) -> Iterator[DLBoardLogger]:
    logger = DLBoardLogger.from_names("shipping", server_url=backend_server.url)
    yield logger
    _abandon(logger)


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

    assert list(backend_server.store.fetch_metrics(_experiment_of(logger), run_ids=None).step) == [0, 1, 2]
    # The server itself records an uploaded artifact's ref asynchronously (see `BlobArtifactStore`),
    # a second or so after accepting the upload -- `finalize`'s guarantee ends at "accepted".
    deadline = time.monotonic() + 10
    while not list(backend_server.store.fetch_artifacts(experiment_id=_experiment_of(logger), run_ids=None)):
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
        _abandon(second)

    assert second.run_id == logger.run_id
    assert [run.id for run in backend_server.store.get_runs(_experiment_of(logger), run_ids=None)] == [
        logger.run_id
    ]
    assert list(backend_server.store.fetch_metrics(_experiment_of(logger), run_ids=None).step) == [0]


def test_a_run_is_pinned_and_kept_the_moment_it_is_launched(backend_server: BackendServer) -> None:
    """A baseline need not wait for someone to click Pin and Keep (#499, #562)."""
    logger = DLBoardLogger.from_names("shipping", server_url=backend_server.url, pin=PinColor.RED, keep=True)
    try:
        run_id = logger.run_id
        assert run_id is not None
        experiment_id, only = _experiment_of(logger), frozenset({run_id})
        pins = backend_server.store.pins_of(experiment_id, run_ids=only)[run_id]

        assert backend_server.store.kept_runs(experiment_id, run_ids=only) == only
        assert [named.pin.color for named in pins] == [PinColor.RED]
    finally:
        _abandon(logger)


def test_a_run_marked_that_does_not_exist_is_a_404_the_client_raises(backend_server: BackendServer) -> None:
    with pytest.raises(requests.HTTPError, match="404"):
        BasicDlboardAPI(backend_server.url).mark_run(999_999, keep=True)


def test_a_metric_without_a_step_is_rejected_where_it_was_logged(logger: DLBoardLogger) -> None:
    with pytest.raises(ValueError, match="explicit step"):
        logger.log_metrics({"loss": 1.0})


def test_a_shipping_process_that_died_is_started_again_and_training_goes_on(
    logger: DLBoardLogger, backend_server: BackendServer
) -> None:
    """Regression: logging after a shipping process died raised, which ended the training run with it."""
    _shipping(logger).artifacts.worker.process.kill()
    _shipping(logger).artifacts.worker.process.join()

    with pytest.warns(UserWarning, match="its shipping process died"):
        logger.log_artifact([_image(0)])  # too soon to start it again: this one is dropped, and says so
    _shipping(logger).artifacts.started_at -= run._RESTART_INTERVAL_SEC
    with pytest.warns(UserWarning, match="started again"):
        logger.log_artifact([_image(1)])
    with pytest.warns(UserWarning, match="dropped 1 artifacts item"):
        logger.finalize("success")

    deadline = time.monotonic() + 10
    while not (
        artifacts := list(
            backend_server.store.fetch_artifacts(experiment_id=_experiment_of(logger), run_ids=None)
        )
    ):
        assert time.monotonic() < deadline, "what was logged after the restart never showed up"
        time.sleep(0.1)
    assert [a.step for a in artifacts] == [1]


def test_a_metric_that_diverged_to_nan_ships_and_does_not_hold_up_the_rest(
    logger: DLBoardLogger, backend_server: BackendServer
) -> None:
    """Regression: `requests` refuses NaN in a JSON body, and the batch holding one was retried forever."""
    logger.log_metrics({"loss": math.nan, "grad": math.inf}, step=0)
    logger.log_metrics({"loss": 1.0, "grad": 2.0}, step=1)
    logger.finalize("success")

    metrics = backend_server.store.fetch_metrics(_experiment_of(logger), run_ids=None)
    assert list(metrics.step) == [0, 1]
    loss = list(metrics.values("loss"))
    assert math.isnan(loss[0])
    assert loss[1] == 1.0
    assert list(metrics.values("grad")) == [math.inf, 2.0]


def test_metrics_for_a_run_deleted_mid_training_are_refused_for_good(
    logger: DLBoardLogger, backend_server: BackendServer
) -> None:
    """Regression: retention deleting a long run answered its metrics with a 500, which a client retries forever."""
    run_id = logger.run_id or 0
    backend_server.store.delete_run(
        run_id, backend_server.store.get_or_create_user(Principal.unverified("ret"))
    )
    metrics = LoggedMetrics(
        metrics={"loss": 1.0},
        step=0,
        experiment_id=_experiment_of(logger),
        run_id=run_id,
        timestamp_utc=pendulum.now("UTC"),
    )

    with pytest.raises(requests.HTTPError, match="404") as refused:
        BasicDlboardAPI(backend_server.url).log_metric_batch([metrics])

    assert classify_failure(refused.value) is Failure.WHOLE


def test_an_artifact_batch_sent_again_is_recorded_once(
    logger: DLBoardLogger, backend_server: BackendServer, tmp_path: Path
) -> None:
    """Regression: a retry of a batch that partly landed recorded its uploads twice, or failed on them for good."""
    run_id, experiment_id = logger.run_id or 0, _experiment_of(logger)
    upload = _image(0).to_artifact(tmp_path, run_id=run_id, experiment_id=experiment_id)
    api = BasicDlboardAPI(backend_server.url)

    api.log_artifact_batch([upload, upload])  # one ref, twice in one batch
    api.log_artifact_batch([upload])  # and sent again, as a client whose answer was lost would

    deadline = time.monotonic() + 10
    while not list(backend_server.store.fetch_artifacts(experiment_id=experiment_id, run_ids=None)):
        assert time.monotonic() < deadline, "the upload never showed up"
        time.sleep(0.1)
    time.sleep(2)  # what the ingest of a second copy would take, were there one
    assert len(list(backend_server.store.fetch_artifacts(experiment_id=experiment_id, run_ids=None))) == 1


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
        _abandon(logger)

    assert not _shipping(logger).artifacts.unflushed, "the kept checkpoints were queued after the last flush"

    deadline = time.monotonic() + 10
    while True:
        artifacts = list(
            backend_server.store.fetch_artifacts(experiment_id=_experiment_of(logger), run_ids=None)
        )
        if {a.key for a in artifacts} >= expected_keys or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert {a.key for a in artifacts} == expected_keys
    assert {a.tags[FILE_KIND_TAG] for a in artifacts} == {FileKind.CHECKPOINT.value}
    assert all(a.fname.endswith(".ckpt") for a in artifacts)


def test_finalize_records_how_the_run_ended(logger: DLBoardLogger, backend_server: BackendServer) -> None:
    """The whole path: the client's `finalize`, the REST route, the store. Which report wins is store-level."""
    run = backend_server.store.get_run(logger.run_id or 0)
    assert run is not None
    assert (run.status, run.ended_at) == (RunStatus.RUNNING, None)

    logger.finalize("success")
    finished = backend_server.store.get_run(run.id)
    assert finished is not None
    assert (finished.status, finished.ended_at is not None) == (RunStatus.FINISHED, True)


def test_the_exit_flush_records_that_the_run_ended_without_saying_how(
    logger: DLBoardLogger, backend_server: BackendServer
) -> None:
    """It can't tell a crash from a script that simply ended -- but it did end, so it is not "running"."""
    logger.finalize("atexit")

    run = backend_server.store.get_run(logger.run_id or 0)
    assert run is not None
    assert (run.status, run.ended_at is not None) == (RunStatus.UNKNOWN, True)


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
        f"{backend_server.url}/{run_path(RunCall.FINISH, str(run_id or logger.run_id))}",
        json=body,
        timeout=10,
    )

    assert res.status_code == expected_status


def test_scalar_and_unit_tags_reach_the_store_as_logged(
    backend_server: BackendServer, tmp_path: Path
) -> None:
    """Expand step of numeric tags: a client that sends numbers (a later release) must not get a 400."""
    api = BasicDlboardAPI(backend_server.url)
    experiment = api.get_or_create_experiment(api.get_or_create_project("p").id, "e")
    run = api.create_run(NewRun(experiment_id=experiment.id))
    path = tmp_path / "a.bin"
    path.write_bytes(b"hello")
    tags: dict[str, TagValue] = {
        "score": 0.25,
        "epoch": 3,
        "best": True,
        "split": "val",
        "mem": Quantity(value=2.5, unit=SizeUnit.GIGABYTES),
    }

    api.log_artifact_batch(
        [
            (
                NewArtifact(
                    key="k", fname=path.name, run_id=run.id, experiment_id=experiment.id, step=0, tags=tags
                ),
                path,
            )
        ]
    )

    deadline = time.monotonic() + 10
    while not (
        stored := list(backend_server.store.fetch_artifacts(experiment_id=experiment.id, run_ids=None))
    ):
        assert time.monotonic() < deadline, "artifact was never recorded"
        time.sleep(0.1)
    assert stored[0].tags == tags
