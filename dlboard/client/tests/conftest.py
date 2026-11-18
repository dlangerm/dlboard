"""Fakes for the client's unit tests: a server that records what it was asked, and shippers that spawn nothing."""

from __future__ import annotations

import dataclasses
import threading
from typing import TYPE_CHECKING, Any

import pytest

from dlboard import models
from dlboard._wire import Identity
from dlboard.client import _context, dlboard_logger, run

if TYPE_CHECKING:
    from dlboard.client._context import ContextSettings, Diff


class FakeAPI:
    """Answers what starting a run asks; an existing run is #7, in experiment 2, and a new one is #99."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.finish_error: Exception | None = None
        self.context_error: Exception | None = None
        self.diff: Diff | None = None
        """What `_context.collect` finds uncommitted, for every run started in the test (nothing, by default)."""

    def collect(
        self, run_id: int, experiment_id: int, settings: ContextSettings
    ) -> tuple[models.NewRunContext, Diff | None]:
        """Stands in for `_context.collect`: no git, no package listing, in a unit test."""
        del settings
        return models.NewRunContext(
            run_id=run_id, experiment_id=experiment_id, hostname="test-host"
        ), self.diff

    def record_run_context(self, context: models.NewRunContext) -> models.RunContext:
        self.calls.append(("record_run_context", (context.run_id,), {"hostname": context.hostname}))
        if self.context_error is not None:
            raise self.context_error
        return models.RunContext(id=1, **context.model_dump())

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

    def log_metric_batch(self, metrics: list[models.LoggedMetrics]) -> None:
        """Only referenced (bound into the stubbed shippers), never called."""

    def create_experiment(self, new_experiment: models.NewExperiment) -> models.Experiment:
        self.calls.append(
            ("create_experiment", (new_experiment.project_id,), {"source": new_experiment.source})
        )
        return models.Experiment(id=2, project_id=new_experiment.project_id, name="new")

    def get_run(self, run_id: int) -> models.Run:
        self.calls.append(("get_run", (run_id,), {}))
        return models.Run(id=run_id, experiment_id=2)

    def finish_run(self, run_id: int, status: models.RunStatus) -> None:
        self.calls.append(("finish_run", (run_id, status), {}))
        if self.finish_error is not None:
            raise self.finish_error

    def create_run(self, new_run: models.NewRun) -> models.Run:
        self.calls.append(("create_run", (new_run.experiment_id,), {"name": new_run.name}))
        return models.Run(id=99, experiment_id=new_run.experiment_id, name=new_run.name)

    def mark_run(self, run_id: int, *, pin: models.PinColor | None = None, keep: bool = False) -> models.Run:
        self.calls.append(("mark_run", (run_id,), {"pin": pin, "keep": keep}))
        return models.Run(id=run_id, experiment_id=2)

    def log_hyperparams(self, hparams: models.NewHyperParams) -> models.HyperParams:
        self.calls.append(("log_hyperparams", (hparams.hparams_dict,), {}))
        return models.HyperParams(id=1, **hparams.model_dump())

    def called(self, name: str) -> list[tuple[Any, ...]]:
        """The positional arguments of every call to `name`, in order."""
        return [args for called, args, _kwargs in self.calls if called == name]


@dataclasses.dataclass
class _ReadyWorker:
    """Already `ready`, so starting a run doesn't wait for a process that never spawns."""

    ready: threading.Event = dataclasses.field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.ready.set()


class FakeShipper:
    """Stands in for `_Shipper`: nothing spawns."""

    def __init__(self) -> None:
        self.worker = _ReadyWorker()
        self.flushed = False
        self.unflushed = False
        self.items: list[Any] = []

    def put(self, item: Any) -> None:  # noqa: ANN401
        """Record what the run queued, instead of shipping it."""
        self.items.append(item)
        self.unflushed = True

    def flush(self, deadline: float) -> bool:
        """So `flush()` (called explicitly, or by an exit hook) has something to call."""
        del deadline
        self.flushed, self.unflushed = True, False
        return True

    def close(self) -> None:
        """What a run nothing uses any more calls on its way out."""


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> FakeAPI:
    """The server every run and logger started in the test talks to, and the context each one finds."""
    api = FakeAPI()

    def _factory(base_url: str) -> FakeAPI:
        del base_url
        return api

    monkeypatch.setattr(run, "BasicDlboardAPI", _factory)
    monkeypatch.setattr(dlboard_logger, "BasicDlboardAPI", _factory)
    monkeypatch.setattr(_context, "collect", api.collect)
    return api


@pytest.fixture
def shippers(monkeypatch: pytest.MonkeyPatch) -> list[FakeShipper]:
    """Every shipper started in the test, in order: a run starts its metrics one, then its artifacts one."""
    started: list[FakeShipper] = []

    def _start(*_args: Any, **_kwargs: Any) -> FakeShipper:  # noqa: ANN401
        started.append(FakeShipper())
        return started[-1]

    monkeypatch.setattr(run, "_Shipper", _start)
    return started
