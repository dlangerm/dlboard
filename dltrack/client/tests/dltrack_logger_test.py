# pyright: reportPrivateUsage=false
"""Unit tests for the logger's name lookup and failure classification, with no real processes/network.

`dltrack/tests/logger_shipping_test.py` covers the real thing end to end against a live server.
"""

from __future__ import annotations

import threading
import warnings
from typing import Any

import pytest
import requests
from pytorch_lightning.utilities import rank_zero_only

from dltrack import models
from dltrack.client import dltrack_logger
from dltrack.client.dltrack_logger import (
    DLTrackLogger,
    DLTrackLoggerSettings,
    is_rejection,
    warn_if_startup_was_slow,
)
from dltrack.plugins.backend.basic_rest_backend import Identity


class _FakeAPI:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def whoami(self) -> Identity:
        return Identity(id=1, username="me")

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


class _FakeShipper:
    """Stands in for `_Shipper`: nothing spawns, and `ready` is already set so `__init__` doesn't wait."""

    def __init__(self) -> None:
        self.ready = threading.Event()
        self.ready.set()


def _stub_shippers(monkeypatch: pytest.MonkeyPatch) -> list[_FakeShipper]:
    started: list[_FakeShipper] = []

    def _start(*_args: Any, **_kwargs: Any) -> _FakeShipper:  # noqa: ANN401
        started.append(_FakeShipper())
        return started[-1]

    monkeypatch.setattr(dltrack_logger, "_start_shipper", _start)
    return started


def _stub_api(monkeypatch: pytest.MonkeyPatch, fake_api: _FakeAPI) -> None:
    """Replace `BasicDltrackAPI` in `dltrack_logger` with a factory that always returns `fake_api`."""

    def _factory(base_url: str) -> _FakeAPI:
        del base_url
        return fake_api

    monkeypatch.setattr(dltrack_logger, "BasicDltrackAPI", _factory)


def _stub_init(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace `DLTrackLogger.__init__` with a spy that just records its kwargs."""
    init_kwargs: dict[str, Any] = {}

    def _fake_init(self: DLTrackLogger, **kwargs: Any) -> None:  # noqa: ANN401
        init_kwargs.update(kwargs)

    monkeypatch.setattr(DLTrackLogger, "__init__", _fake_init)
    return init_kwargs


def test_from_names_looks_up_project_and_experiment_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_api = _FakeAPI("http://x")
    _stub_api(monkeypatch, fake_api)
    init_kwargs = _stub_init(monkeypatch)

    DLTrackLogger.from_names("proj", experiment_name="exp", project_description="desc", server_url="http://x")

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

    DLTrackLogger.from_names("proj")

    assert fake_api.calls[1] == (
        "get_or_create_experiment",
        (1,),
        {"name": "default", "source": models.ExperimentSource.PYTORCH_LIGHTNING},
    )


def test_from_names_passes_settings_through(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_api = _FakeAPI("http://x")
    _stub_api(monkeypatch, fake_api)
    init_kwargs = _stub_init(monkeypatch)
    settings = DLTrackLoggerSettings(metrics_q_size=5)

    DLTrackLogger.from_names("proj", settings=settings)

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
        monkeypatch.setenv("DLTRACK_RUN_ID", run_id_env)

    logger = DLTrackLogger(project_id=1, experiment_id=None, run_id=run_id_arg)

    created = [call for call in fake_api.calls if call[0] == "create_run"]
    assert bool(created) is creates_a_run
    assert logger.run_id == (99 if creates_a_run else 7)
    # An attached run's own experiment is used, instead of creating a new experiment around it.
    assert ("create_experiment" in {call[0] for call in fake_api.calls}) is creates_a_run


def test_a_run_from_another_experiment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_api(monkeypatch, _FakeRunAPI("http://x"))
    _stub_shippers(monkeypatch)

    with pytest.raises(ValueError, match="belongs to experiment 2, not 5"):
        DLTrackLogger(project_id=1, experiment_id=5, run_id=7)


def test_a_non_zero_rank_makes_no_server_calls_and_logs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_api(base_url: str) -> None:
        msg = f"a non-zero rank must not talk to the server ({base_url})"
        raise AssertionError(msg)

    monkeypatch.setattr(dltrack_logger, "BasicDltrackAPI", _no_api)
    monkeypatch.setattr(rank_zero_only, "rank", 1, raising=False)
    started = _stub_shippers(monkeypatch)

    logger = DLTrackLogger.from_names("proj", run_id=7)
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


def test_warn_if_startup_was_slow_warns_past_the_threshold() -> None:
    with pytest.warns(UserWarning, match="took 3.0s to start up"):
        warn_if_startup_was_slow(3.0)


def test_warn_if_startup_was_slow_is_silent_within_the_threshold() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        warn_if_startup_was_slow(0.1)
