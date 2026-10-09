# pyright: reportPrivateUsage=false
"""Unit tests for the logger's name lookup and failure classification, with no real processes/network.

`dlboard/tests/logger_shipping_test.py` covers the real thing end to end against a live server.
"""

from __future__ import annotations

import dataclasses
import io
import queue
import threading
import warnings
from argparse import Namespace
from enum import Enum
from typing import TYPE_CHECKING, Any

import matplotlib.figure
import numpy as np
import PIL.Image
import pydantic
import pydantic.dataclasses
import pytest
import pytorch_lightning as pl
import requests
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.demos.boring_classes import BoringModel
from pytorch_lightning.utilities import rank_zero_only

from dlboard import models
from dlboard.client import dlboard_logger
from dlboard.client._rest_api import Identity
from dlboard.client.artifacts.figure import Figure
from dlboard.client.artifacts.file import File
from dlboard.client.dlboard_logger import (
    DLBoardLogger,
    DLBoardLoggerSettings,
    flatten_hparams,
    is_oversized,
    is_rejection,
    warn_if_startup_was_slow,
)

if TYPE_CHECKING:
    from pathlib import Path


class _FakeAPI:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def whoami(self) -> Identity:
        return Identity(id=1, username="me", server_version="0.0.0-test")

    def get_or_create_project(self, name: str, description: str = "") -> models.Project:
        self.calls.append(("get_or_create_project", (name,), {"description": description}))
        return models.Project(id=1, name=name, description=description)

    def get_or_create_experiment(
        self, project_id: int, name: str = "default", source: models.ExperimentSource | None = None
    ) -> models.Experiment:
        self.calls.append(("get_or_create_experiment", (project_id,), {"name": name, "source": source}))
        return models.Experiment(id=2, project_id=project_id, name=name, source=source)


class _FakeRunAPI(_FakeAPI):
    """Adds what `__init__` itself (not just `from_names`) calls: an existing run is #7, in experiment 2."""

    def log_metric_batch(self, metrics: list[models.LoggedMetrics]) -> None:
        """Only referenced (bound into the stubbed shippers), never called."""

    def create_experiment(self, new_experiment: models.NewExperiment) -> models.Experiment:
        self.calls.append(("create_experiment", (new_experiment.project_id,), {}))
        return models.Experiment(id=2, project_id=new_experiment.project_id, name="new")

    def get_run(self, run_id: int) -> models.Run:
        self.calls.append(("get_run", (run_id,), {}))
        return models.Run(id=run_id, experiment_id=2)

    def create_run(self, run: models.NewRun) -> models.Run:
        self.calls.append(("create_run", (run.experiment_id,), {}))
        return models.Run(id=99, experiment_id=run.experiment_id)

    def log_hyperparams(self, hparams: models.NewHyperParams) -> None:
        self.calls.append(("log_hyperparams", (hparams.hparams_dict,), {}))


class _FakeShipper:
    """Stands in for `_Shipper`: nothing spawns, and `ready` is already set so `__init__` doesn't wait."""

    def __init__(self) -> None:
        self.ready = threading.Event()
        self.ready.set()
        self.flushed = False
        self.items: list[Any] = []

    def put(self, item: Any) -> None:  # noqa: ANN401
        """Record what the logger queued, instead of shipping it."""
        self.items.append(item)

    def flush(self) -> None:
        """So `finalize()` (called explicitly, or by the logger's own atexit hook) has something to call."""
        self.flushed = True


def _stub_shippers(monkeypatch: pytest.MonkeyPatch) -> list[_FakeShipper]:
    started: list[_FakeShipper] = []

    def _start(*_args: Any, **_kwargs: Any) -> _FakeShipper:  # noqa: ANN401
        started.append(_FakeShipper())
        return started[-1]

    monkeypatch.setattr(dlboard_logger, "_start_shipper", _start)
    return started


def _stub_api(monkeypatch: pytest.MonkeyPatch, fake_api: _FakeAPI) -> None:
    """Replace `BasicDlboardAPI` in `dlboard_logger` with a factory that always returns `fake_api`."""

    def _factory(base_url: str) -> _FakeAPI:
        del base_url
        return fake_api

    monkeypatch.setattr(dlboard_logger, "BasicDlboardAPI", _factory)


def _stub_init(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace `DLBoardLogger.__init__` with a spy that just records its kwargs."""
    init_kwargs: dict[str, Any] = {}

    def _fake_init(self: DLBoardLogger, **kwargs: Any) -> None:  # noqa: ANN401
        init_kwargs.update(kwargs)

    monkeypatch.setattr(DLBoardLogger, "__init__", _fake_init)
    return init_kwargs


def test_from_names_looks_up_project_and_experiment_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_api = _FakeAPI("http://x")
    _stub_api(monkeypatch, fake_api)
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


def test_from_names_defaults_the_experiment_name_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_api = _FakeAPI("http://x")
    _stub_api(monkeypatch, fake_api)
    _stub_init(monkeypatch)

    DLBoardLogger.from_names("proj")

    assert fake_api.calls[1] == (
        "get_or_create_experiment",
        (1,),
        {"name": "default", "source": models.ExperimentSource.PYTORCH_LIGHTNING},
    )


def test_from_names_passes_settings_through(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_api = _FakeAPI("http://x")
    _stub_api(monkeypatch, fake_api)
    init_kwargs = _stub_init(monkeypatch)
    settings = DLBoardLoggerSettings(metrics_q_size=5)

    DLBoardLogger.from_names("proj", settings=settings)

    assert init_kwargs["settings"] is settings


@pytest.mark.parametrize(
    ("run_id_arg", "run_id_env", "creates_a_run"),
    [(7, None, False), (None, "7", False), (None, None, True)],
    ids=["argument", "env-var", "neither"],
)
def test_a_logger_attaches_to_the_run_it_is_given_and_otherwise_creates_one(
    monkeypatch: pytest.MonkeyPatch, run_id_arg: int | None, run_id_env: str | None, *, creates_a_run: bool
) -> None:
    fake_api = _FakeRunAPI("http://x")
    _stub_api(monkeypatch, fake_api)
    _stub_shippers(monkeypatch)
    if run_id_env is not None:
        monkeypatch.setenv("DLBOARD_RUN_ID", run_id_env)

    logger = DLBoardLogger(project_id=1, experiment_id=None, run_id=run_id_arg)

    created = [call for call in fake_api.calls if call[0] == "create_run"]
    assert bool(created) is creates_a_run
    assert logger.run_id == (99 if creates_a_run else 7)
    # An attached run's own experiment is used, instead of creating a new experiment around it.
    assert ("create_experiment" in {call[0] for call in fake_api.calls}) is creates_a_run
    logger.finalize("success")  # marks it finalized, so its atexit hook is a no-op at session end


def test_a_run_from_another_experiment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_api(monkeypatch, _FakeRunAPI("http://x"))
    _stub_shippers(monkeypatch)

    with pytest.raises(ValueError, match="belongs to experiment 2, not 5"):
        DLBoardLogger(project_id=1, experiment_id=5, run_id=7)


def test_log_figure_queues_a_png_rendered_at_the_call_site(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_api(monkeypatch, _FakeRunAPI("http://x"))
    started = _stub_shippers(monkeypatch)
    logger = DLBoardLogger(project_id=1, experiment_id=None)
    _metrics, artifacts = started
    fig = matplotlib.figure.Figure()
    fig.subplots().plot([1, 2, 3])  # pyright: ignore[reportUnknownMemberType]

    logger.log_figure(fig, "loss_curve", step=4, tags={"split": "val"}, save_kwargs={"dpi": 50})
    fig.clear()  # callers close or reuse the figure right after logging -- the queued PNG mustn't change

    (queued,) = artifacts.items
    (figure,) = queued
    assert isinstance(figure, Figure)
    assert (figure.key, figure.step, figure.tags) == ("loss_curve", 4, {"split": "val"})
    assert figure.format == "png"
    assert PIL.Image.open(io.BytesIO(figure.data)).size == (320, 240)
    logger.finalize("success")


def test_a_non_zero_rank_makes_no_server_calls_and_logs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_api(base_url: str) -> None:
        msg = f"a non-zero rank must not talk to the server ({base_url})"
        raise AssertionError(msg)

    monkeypatch.setattr(dlboard_logger, "BasicDlboardAPI", _no_api)
    monkeypatch.setattr(rank_zero_only, "rank", 1, raising=False)
    started = _stub_shippers(monkeypatch)

    logger = DLBoardLogger.from_names("proj", run_id=7)
    logger.log_metrics({"loss": 1.0})  # no step: raises on rank 0, silently dropped here
    logger.finalize("success")

    assert started == []
    assert logger.run_id == 7


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


@pytest.mark.parametrize(
    ("failure", "rejected"),
    [
        (_http_error(400), True),
        (_http_error(422), True),
        (_http_error(503), False),
        (_http_error(408), False),
        (_http_error(429), False),
        (requests.ConnectionError(), False),
    ],
)
def test_is_rejection_only_treats_a_4xx_that_can_never_succeed_as_permanent(
    failure: Exception, *, rejected: bool
) -> None:
    assert is_rejection(failure) is rejected


@pytest.mark.parametrize(
    ("failure", "oversized"),
    [(_http_error(413), True), (_http_error(400), False), (requests.ConnectionError(), False)],
)
def test_is_oversized_only_matches_413(failure: Exception, *, oversized: bool) -> None:
    assert is_oversized(failure) is oversized


class _FakeProcess:
    """`alive` is a plain mutable attribute, not init-only -- a real process can die mid-test too."""

    def __init__(self, *, alive: bool) -> None:
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive


def _shipper(*, alive: bool = True, maxsize: int = 0) -> dlboard_logger._Shipper[str]:
    """
    A real `_Shipper` over test doubles: a plain `queue.Queue` (not a real `multiprocessing.Queue`,
    which needs a spawned process on the other end) and `threading.Event` (not
    `multiprocessing.synchronize.Event`) -- structurally identical for every method `_Shipper` calls.
    """
    q: queue.Queue[str | None] = queue.Queue(maxsize=maxsize)
    shipper: dlboard_logger._Shipper[str] = dlboard_logger._Shipper(  # pyright: ignore[reportUnknownVariableType]
        "test",
        q,  # pyright: ignore[reportArgumentType]
        _FakeProcess(alive=alive),  # pyright: ignore[reportArgumentType]
        threading.Event(),  # pyright: ignore[reportArgumentType]
        threading.Event(),  # pyright: ignore[reportArgumentType]
    )
    return shipper


def test_shipper_put_drops_rather_than_blocks_once_the_queue_is_full() -> None:
    """Regression: training must never stall on dlboard falling behind -- the old blocking `put` could."""
    shipper = _shipper(maxsize=1)
    shipper.put("a")

    with pytest.warns(UserWarning, match="queue is full"):
        shipper.put("b")

    assert shipper.queue.get_nowait() == "a"
    assert shipper.queue.empty()  # "b" was dropped, never queued
    assert shipper._dropped_total == 1


def test_shipper_put_raises_if_the_shipping_process_already_died() -> None:
    shipper = _shipper(alive=False)

    with pytest.raises(RuntimeError, match="died"):
        shipper.put("a")


def test_shipper_flush_warns_and_returns_instead_of_hanging_on_a_dead_process() -> None:
    shipper = _shipper(alive=False)

    with pytest.warns(UserWarning, match="dead"):
        shipper.flush()

    assert shipper.queue.empty()  # no flush sentinel was queued for nothing to ever drain


def test_shipper_flush_warns_once_with_the_total_dropped_this_run() -> None:
    shipper = _shipper(maxsize=1)
    shipper.put("a")
    with pytest.warns(UserWarning, match="queue is full"):
        shipper.put("b")
    shipper.put("c")  # dropped too, but rate-limited: no second warning right after the first

    # Dead now, so `flush()` doesn't block waiting on `flushed` -- nothing would ever set it here.
    assert isinstance(shipper.process, _FakeProcess)
    shipper.process.alive = False
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        shipper.flush()

    assert any("dropped 2 test" in str(w.message) for w in caught)


def test_warn_if_startup_was_slow_warns_past_the_threshold() -> None:
    with pytest.warns(UserWarning, match="took 3.0s to start up"):
        warn_if_startup_was_slow(3.0)


def test_warn_if_startup_was_slow_is_silent_within_the_threshold() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        warn_if_startup_was_slow(0.1)


class _Optimizer(Enum):
    ADAMW = "adamw"


class _Schedule(pydantic.BaseModel):
    kind: _Optimizer = _Optimizer.ADAMW
    betas: tuple[float, float] = (0.9, 0.999)


@pydantic.dataclasses.dataclass
class _PydanticDataclass:
    warmup: int = 100


@dataclasses.dataclass
class _StdlibDataclass:
    flip: bool = True


class _Config(pydantic.BaseModel):
    lr: float = 1e-3
    schedule: _Schedule = _Schedule()
    warmup: _PydanticDataclass = _PydanticDataclass()
    renamed: int = pydantic.Field(default=3, serialization_alias="alias")

    @pydantic.field_serializer("lr")
    def _as_text(self, lr: float) -> str:
        return f"{lr:.0e}"


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        pytest.param(
            {"hidden": 128, "name": "a", "n": "128"}, {"hidden": 128, "name": "a", "n": "128"}, id="flat"
        ),
        pytest.param(
            {"fraction": np.float32(0.5), "n": np.int64(3)}, {"fraction": 0.5, "n": 3}, id="numpy-scalars"
        ),
        pytest.param(Namespace(a=1, b=Namespace(c="x")), {"a": 1, "b/c": "x"}, id="namespace"),
        pytest.param(
            {"opt": _Optimizer.ADAMW, "ids": [1, 2]}, {"opt": "adamw", "ids": "[1, 2]"}, id="enum-and-list"
        ),
        pytest.param({"aug": _StdlibDataclass()}, {"aug/flip": True}, id="stdlib-dataclass"),
        pytest.param({"data": _PydanticDataclass(warmup=5)}, {"data/warmup": 5}, id="pydantic-dataclass"),
        pytest.param(
            {"optim": _Config(), "seed": 1},
            {
                "optim/lr": "1e-03",
                "optim/schedule/kind": "adamw",
                "optim/schedule/betas": "[0.9, 0.999]",
                "optim/warmup/warmup": 100,
                "optim/alias": 3,
                "seed": 1,
            },
            id="nested-pydantic-honours-its-own-serializers",
        ),
        pytest.param({"layer": _Config}, {"layer": str(_Config)}, id="unserializable-becomes-text"),
    ],
)
def test_flatten_hparams_joins_nested_configs_with_slashes(params: Any, expected: dict[str, Any]) -> None:  # noqa: ANN401
    assert flatten_hparams(params) == expected


def test_log_hyperparams_sends_a_nested_config_flattened(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _FakeRunAPI("http://x")
    _stub_api(monkeypatch, api)
    _stub_shippers(monkeypatch)
    logger = DLBoardLogger(project_id=1, experiment_id=None)

    logger.log_hyperparams({"optim": _Schedule(), "seed": 1})

    assert (
        "log_hyperparams",
        ({"optim/kind": "adamw", "optim/betas": "[0.9, 0.999]", "seed": 1},),
        {},
    ) in api.calls
    logger.finalize("success")


def test_every_finalize_flushes_what_was_logged_since_the_last_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lightning finalizes after `fit` and again after `test`; the second one's metrics must ship too."""
    _stub_api(monkeypatch, _FakeRunAPI("http://x"))
    started = _stub_shippers(monkeypatch)
    logger = DLBoardLogger(project_id=1, experiment_id=None)
    logger.log_metrics({"fit/loss": 1.0}, step=0)
    logger.finalize("success")
    for shipper in started:
        shipper.flushed = False

    logger._finalize_at_exit()  # nothing new was logged: no warning, no flush
    assert not any(shipper.flushed for shipper in started)

    logger.log_metrics({"test/loss": 1.0}, step=1)
    logger.finalize("success")

    assert all(shipper.flushed for shipper in started)


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


def _uploaded_files(artifacts: _FakeShipper) -> list[File]:
    return [artifact for batch in artifacts.items for artifact in batch if isinstance(artifact, File)]


_QUIET_LIGHTNING = pytest.mark.filterwarnings(
    "ignore::lightning_fabric.utilities.warnings.PossibleUserWarning",
    r"ignore:.isinstance\(treespec, LeafSpec\).:FutureWarning",
)


@_QUIET_LIGHTNING
def test_each_checkpoint_is_stamped_with_the_step_it_was_saved_at(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    `last.ckpt` is rewritten every epoch, and a (key, step) pair can be stored only once: the server
    rejects a second upload as a duplicate and the client then retries that batch forever. With no
    metrics logged there is no step to borrow from them, so each save carries its own.
    """
    _stub_api(monkeypatch, _FakeRunAPI("http://x"))
    started = _stub_shippers(monkeypatch)
    logger = DLBoardLogger(project_id=1, experiment_id=None, log_model="all")
    _metrics, artifacts = started

    _fit_with(logger, tmp_path, ModelCheckpoint(dirpath=tmp_path / "ckpts", save_top_k=1, save_last=True))

    steps = {(f.key, f.step) for f in _uploaded_files(artifacts)}
    assert [step for key, step in sorted(steps) if key == "checkpoints/last.ckpt"] == [2, 4, 6]
    assert len(steps) == len(_uploaded_files(artifacts))


@_QUIET_LIGHTNING
def test_every_checkpoint_callback_gets_its_kept_checkpoints_uploaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_api(monkeypatch, _FakeRunAPI("http://x"))
    started = _stub_shippers(monkeypatch)
    logger = DLBoardLogger(project_id=1, experiment_id=None, log_model=True)
    _metrics, artifacts = started

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
