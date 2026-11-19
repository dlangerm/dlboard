"""
Unit tests for what the Lightning adapter adds to a `Run`, with no real processes or network (see `conftest.py`).

What a run does with what is logged is `run_test.py`'s; `dlboard/tests/logger_shipping_test.py` covers the real
thing end to end against a live server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import pytorch_lightning as pl
import requests
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.demos.boring_classes import BoringModel
from pytorch_lightning.utilities import rank_zero_only

from dlboard import models
from dlboard.client import dlboard_logger
from dlboard.client.artifacts.file import File
from dlboard.client.dlboard_logger import DLBoardLogger, DLBoardLoggerSettings

if TYPE_CHECKING:
    from pathlib import Path

    from dlboard.client.tests.conftest import FakeAPI, FakeShipper


def _stub_init(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace `DLBoardLogger.__init__` with a spy that just records its kwargs."""
    init_kwargs: dict[str, Any] = {}

    def _fake_init(self: DLBoardLogger, **kwargs: Any) -> None:  # noqa: ANN401
        init_kwargs.update(kwargs)

    monkeypatch.setattr(DLBoardLogger, "__init__", _fake_init)
    return init_kwargs


def test_from_names_looks_up_project_and_experiment_by_name(
    monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI
) -> None:
    init_kwargs = _stub_init(monkeypatch)

    DLBoardLogger.from_names("proj", experiment_name="exp", project_description="desc", server_url="http://x")

    assert fake_api.calls == [
        ("get_or_create_project", ("proj",), {"description": "desc"}),
        (
            "get_or_create_experiment",
            (1,),
            {"name": "exp", "source": models.ExperimentSource.PYTORCH_LIGHTNING},
        ),
    ]
    assert init_kwargs["project_id"] == 1
    assert init_kwargs["experiment_id"] == 2
    assert init_kwargs["server_url"] == "http://x"


def test_from_names_defaults_the_experiment_name_to_default(
    monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI
) -> None:
    _stub_init(monkeypatch)

    DLBoardLogger.from_names("proj")

    assert fake_api.calls[1] == (
        "get_or_create_experiment",
        (1,),
        {"name": "default", "source": models.ExperimentSource.PYTORCH_LIGHTNING},
    )


def test_from_names_passes_settings_through(monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI) -> None:
    del fake_api
    init_kwargs = _stub_init(monkeypatch)
    settings = DLBoardLoggerSettings(metrics_q_size=5)

    DLBoardLogger.from_names("proj", settings=settings)

    assert init_kwargs["settings"] is settings


@pytest.mark.parametrize(
    ("run_id_arg", "run_id_env", "creates_a_run"),
    [(7, None, False), (None, "7", False), (None, None, True)],
    ids=["argument", "env-var", "neither"],
)
def test_a_logger_attaches_to_the_run_it_is_given_and_otherwise_creates_one(  # noqa: PLR0913
    monkeypatch: pytest.MonkeyPatch,
    fake_api: FakeAPI,
    shippers: list[FakeShipper],
    run_id_arg: int | None,
    run_id_env: str | None,
    *,
    creates_a_run: bool,
) -> None:
    del shippers
    if run_id_env is not None:
        monkeypatch.setenv("DLBOARD_RUN_ID", run_id_env)

    logger = DLBoardLogger(project_id=1, experiment_id=None, run_id=run_id_arg)

    assert bool(fake_api.called("create_run")) is creates_a_run
    assert logger.run_id == (99 if creates_a_run else 7)
    # An attached run's own experiment is used, instead of creating a new experiment around it.
    assert bool(fake_api.called("create_experiment")) is creates_a_run
    logger.finalize("success")  # marks it finalized, so its atexit hook is a no-op at session end


def test_a_new_experiment_says_it_was_logged_through_lightning(
    fake_api: FakeAPI, shippers: list[FakeShipper]
) -> None:
    del shippers
    logger = DLBoardLogger(project_id=1)

    ((_, _, kwargs),) = [call for call in fake_api.calls if call[0] == "create_experiment"]
    assert kwargs["source"] is models.ExperimentSource.PYTORCH_LIGHTNING
    logger.finalize("success")


def test_a_run_from_another_experiment_is_rejected(fake_api: FakeAPI, shippers: list[FakeShipper]) -> None:
    del fake_api, shippers
    with pytest.raises(ValueError, match="belongs to experiment 2, not 5"):
        DLBoardLogger(project_id=1, experiment_id=5, run_id=7)


def test_a_metric_without_a_step_is_rejected_at_the_call(
    fake_api: FakeAPI, shippers: list[FakeShipper]
) -> None:
    del fake_api
    logger = DLBoardLogger(project_id=1)

    with pytest.raises(ValueError, match="explicit step"):
        logger.log_metrics({"loss": 1.0})

    assert shippers[0].items == []
    logger.finalize("success")


def test_a_non_zero_rank_makes_no_server_calls_and_logs_nothing(
    monkeypatch: pytest.MonkeyPatch, shippers: list[FakeShipper]
) -> None:
    def _no_api(server_url: str) -> None:
        msg = f"a non-zero rank must not talk to the server ({server_url})"
        raise AssertionError(msg)

    monkeypatch.setattr(dlboard_logger, "connect", _no_api)
    monkeypatch.setattr(rank_zero_only, "rank", 1, raising=False)

    logger = DLBoardLogger.from_names("proj", run_id=7)
    logger.log_metrics({"loss": 1.0})  # no step: raises on rank 0, silently dropped here
    logger.finalize("success")

    assert shippers == []
    assert logger.run_id == 7


def test_every_finalize_flushes_what_was_logged_since_the_last_one(
    fake_api: FakeAPI, shippers: list[FakeShipper]
) -> None:
    """Lightning finalizes after `fit` and again after `test`; the second one's metrics must ship too."""
    del fake_api
    logger = DLBoardLogger(project_id=1, experiment_id=None)
    logger.log_metrics({"fit/loss": 1.0}, step=0)
    logger.finalize("success")
    for shipper in shippers:
        shipper.flushed = False

    logger._finalize_at_exit()  # nothing new was logged: no warning, no flush
    assert not any(shipper.flushed for shipper in shippers)

    logger.log_metrics({"test/loss": 1.0}, step=1)
    logger.finalize("success")

    assert all(shipper.flushed for shipper in shippers)


def _fit_with(logger: DLBoardLogger, tmp_path: Path, *checkpoints: ModelCheckpoint) -> None:
    """Three epochs of two steps each, on CPU, with nothing logged -- so no metric step to borrow."""
    pl.Trainer(
        max_epochs=3,
        limit_train_batches=2,
        limit_val_batches=0,
        accelerator="cpu",
        logger=logger,
        callbacks=list(checkpoints),
        default_root_dir=tmp_path,
        enable_progress_bar=False,
        enable_model_summary=False,
    ).fit(BoringModel())


def _uploaded_files(artifacts: FakeShipper) -> list[File]:
    return [artifact for batch in artifacts.items for artifact in batch if isinstance(artifact, File)]


_QUIET_LIGHTNING = pytest.mark.filterwarnings(
    "ignore::lightning_fabric.utilities.warnings.PossibleUserWarning",
    r"ignore:.isinstance\(treespec, LeafSpec\).:FutureWarning",
)


@_QUIET_LIGHTNING
def test_each_checkpoint_is_stamped_with_the_step_it_was_saved_at(
    fake_api: FakeAPI, shippers: list[FakeShipper], tmp_path: Path
) -> None:
    """
    `last.ckpt` is rewritten every epoch, and a (key, step) pair can be stored only once: the server
    rejects a second upload as a duplicate and the client then retries that batch forever. With no
    metrics logged there is no step to borrow from them, so each save carries its own.
    """
    del fake_api
    logger = DLBoardLogger(project_id=1, experiment_id=None, log_model="all")
    _metrics, artifacts = shippers

    _fit_with(logger, tmp_path, ModelCheckpoint(dirpath=tmp_path / "ckpts", save_top_k=1, save_last=True))

    steps = {(f.key, f.step) for f in _uploaded_files(artifacts)}
    assert [step for key, step in sorted(steps) if key == "checkpoints/last.ckpt"] == [2, 4, 6]
    assert len(steps) == len(_uploaded_files(artifacts))


@_QUIET_LIGHTNING
def test_every_checkpoint_callback_gets_its_kept_checkpoints_uploaded(
    fake_api: FakeAPI, shippers: list[FakeShipper], tmp_path: Path
) -> None:
    del fake_api
    logger = DLBoardLogger(project_id=1, experiment_id=None, log_model=True)
    _metrics, artifacts = shippers

    _fit_with(
        logger,
        tmp_path,
        ModelCheckpoint(dirpath=tmp_path / "a", filename="first-{epoch}", save_top_k=1),
        ModelCheckpoint(dirpath=tmp_path / "b", filename="second-{epoch}", save_top_k=1, every_n_epochs=2),
    )

    assert {f.key for f in _uploaded_files(artifacts)} == {
        "checkpoints/first-epoch=2.ckpt",
        "checkpoints/second-epoch=1.ckpt",
    }


@pytest.mark.parametrize(
    ("lightning_status", "reported"),
    [
        pytest.param("success", [models.RunStatus.FINISHED], id="success"),
        pytest.param("failed", [models.RunStatus.FAILED], id="failed"),
        pytest.param(
            "atexit", [models.RunStatus.UNKNOWN], id="the-exit-flush-cannot-tell-a-crash-from-an-end"
        ),
        pytest.param("interrupted", [], id="anything-else"),
    ],
)
def test_finalize_reports_how_the_run_ended(
    fake_api: FakeAPI, shippers: list[FakeShipper], lightning_status: str, reported: list[models.RunStatus]
) -> None:
    del shippers
    logger = DLBoardLogger(project_id=1, experiment_id=None)

    logger.finalize(lightning_status)

    assert [status for _run_id, status in fake_api.called("finish_run")] == reported
    if not reported:
        logger.finalize("success")


def test_a_server_that_cannot_record_the_status_never_fails_the_script(
    fake_api: FakeAPI, shippers: list[FakeShipper]
) -> None:
    del shippers
    fake_api.finish_error = requests.ConnectionError("down")
    logger = DLBoardLogger(project_id=1, experiment_id=None)

    with pytest.warns(UserWarning, match="could not record that the run finished"):
        logger.finalize("success")

    fake_api.finish_error = None
    logger.finalize("success")


def test_the_exit_flush_does_not_overwrite_an_outcome_the_run_already_reported(
    fake_api: FakeAPI, shippers: list[FakeShipper]
) -> None:
    del shippers
    logger = DLBoardLogger(project_id=1, experiment_id=None)

    logger.finalize("success")
    logger.finalize("atexit")

    assert [status for _run_id, status in fake_api.called("finish_run")] == [models.RunStatus.FINISHED]


def test_a_logger_that_was_never_finalized_reports_an_unknown_outcome_at_exit(
    fake_api: FakeAPI, shippers: list[FakeShipper]
) -> None:
    """A script that exits without Lightning finalizing it would otherwise read as "running" forever."""
    del shippers
    logger = DLBoardLogger(project_id=1, experiment_id=None)

    with pytest.warns(UserWarning, match="never finalized"):
        logger._finalize_at_exit()

    assert [status for _run_id, status in fake_api.called("finish_run")] == [models.RunStatus.UNKNOWN]
