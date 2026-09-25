"""The pytorch lightning logger for use with dltrack."""

from __future__ import annotations

import contextlib
import functools
import logging
import tempfile
import time
import warnings
from argparse import Namespace
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from queue import Empty
from typing import TYPE_CHECKING, Any, Final, NamedTuple, Protocol, override

import pendulum
import requests
from pydantic import BaseModel
from pytorch_lightning.loggers import Logger

from dltrack import models
from dltrack._mp_context import SPAWN_CONTEXT
from dltrack.plugins.backend.basic_rest_backend import DEFAULT_SERVER_URL, BasicDltrackAPI

DEFAULT_EXPERIMENT_NAME = "default"

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from multiprocessing import Queue
    from multiprocessing.context import SpawnProcess
    from multiprocessing.synchronize import Event as MPEvent

    from dltrack.models._artifact import AnyArtifact


_log = logging.getLogger(__name__)

_STARTUP_WARN_THRESHOLD_SEC = 2.0
"""How long a worker process's import + startup can take before we warn it's the likely cause of
any backpressure warnings seen right after training starts, rather than dltrack itself."""
_STARTUP_WAIT_TIMEOUT_SEC = 60.0
"""Cap on how long `__init__` waits for a worker to report ready, so a wedged import can't hang forever."""
_FLUSH_TIMEOUT_SEC = 60.0
"""Cap on how long `finalize` waits for a worker to ship everything queued, so a dead server can't hang it."""
_SHIP_INTERVAL_SEC = 5.0
"""How long a worker lets a partial batch sit before shipping it anyway."""
_TRANSIENT_CLIENT_ERRORS: Final = frozenset({HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS})


def warn_if_startup_was_slow(elapsed_sec: float) -> None:
    """Warn that any backpressure warnings so far are from worker startup, not sustained throughput."""
    if elapsed_sec > _STARTUP_WARN_THRESHOLD_SEC:
        warnings.warn(
            f"dltrack's logging worker processes took {elapsed_sec:.1f}s to start up. This is "
            "almost always import overhead from your training script or its other dependencies, not "
            "dltrack -- any backpressure warnings logged in the next few steps are a side effect of "
            "this startup delay rather than an ongoing throughput problem.",
            stacklevel=2,
        )


class LogProcParams(NamedTuple):
    """Process parameters for a logger daemon."""

    flush_size: int
    ready: MPEvent
    flushed: MPEvent
    wait_sec: float = _SHIP_INTERVAL_SEC


class Receiver[T](Protocol):
    """The one queue method `ship_batches` needs -- both `multiprocessing` and `queue` queues have it."""

    def get(self, block: bool = True, timeout: float | None = None) -> T: ...  # noqa: D102, FBT001, FBT002


def _is_rejection(exc: Exception) -> bool:
    """Whether the server refused a batch outright -- resending the exact same batch can never succeed."""
    if not isinstance(exc, requests.HTTPError) or exc.response is None:
        return False
    status = exc.response.status_code
    return HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR and (
        status not in _TRANSIENT_CLIENT_ERRORS
    )


def ship_batches[T](q: Receiver[T | None], ship: Callable[[list[T]], None], params: LogProcParams) -> None:
    """
    Drain `q` into batches, shipping one once it's full, `wait_sec` old, or a flush asks for it.

    A batch the server rejects (4xx) is dropped: resending it can never succeed, and keeping it would
    wedge every later item behind it. Any other failure (connection error, 5xx) is transient -- the
    batch is kept, keeps growing with whatever arrives meanwhile, and is retried after `wait_sec`.
    A `None` queued in place of an item requests a flush, acknowledged (`params.flushed`) only once
    everything queued before it is gone.
    """
    params.ready.set()
    batch: list[T] = []
    flush_requested = False
    last_attempt = time.perf_counter()
    while True:
        with contextlib.suppress(Empty):
            item = q.get(timeout=params.wait_sec / 10)
            if item is None:
                flush_requested = True
            else:
                batch.append(item)
        due = flush_requested or len(batch) >= params.flush_size
        if batch and (due or time.perf_counter() - last_attempt > params.wait_sec):
            try:
                ship(batch)
                batch = []
            except Exception as exc:  # noqa: BLE001 -- nothing may kill the shipping process
                if _is_rejection(exc):
                    _log.exception("The server rejected a batch of %s, dropping it", len(batch))
                    batch = []
                else:
                    _log.exception("Failed to ship a batch of %s, retrying", len(batch))
                    time.sleep(params.wait_sec)
            last_attempt = time.perf_counter()
        if flush_requested and not batch:
            flush_requested = False
            params.flushed.set()


def _ship_artifacts(
    api: BasicDltrackAPI, experiment_id: int, run_id: int, batch: list[Sequence[AnyArtifact]]
) -> None:
    """Encode `batch` into a scratch dir that's deleted once it's uploaded, then upload it."""
    with tempfile.TemporaryDirectory() as tmp:
        pairs = [
            artifact.to_artifact(Path(tmp), run_id=run_id, experiment_id=experiment_id)
            for artifacts in batch
            for artifact in artifacts
        ]
        api.log_artifact_batch([new for new, _ in pairs], [path for _, path in pairs])


@dataclass(frozen=True)
class _Shipper[T]:
    """One background process running `ship_batches`, plus the queue feeding it."""

    name: str
    queue: Queue[T | None]
    process: SpawnProcess
    ready: MPEvent
    flushed: MPEvent

    def put(self, item: T) -> None:
        if not self.process.is_alive():
            msg = f"dltrack's {self.name} shipping process died, refusing to back up its queue"
            raise RuntimeError(msg)
        if self.queue.full():
            warnings.warn(
                f"Backpressure on dltrack's {self.name} queue, this will impact iteration speed", stacklevel=3
            )
        self.queue.put(item)

    def flush(self) -> None:
        """Block until everything queued so far has been shipped (or rejected by the server)."""
        self.flushed.clear()
        self.queue.put(None)
        if not self.flushed.wait(_FLUSH_TIMEOUT_SEC):
            warnings.warn(
                f"dltrack's {self.name} still hadn't all been shipped after {_FLUSH_TIMEOUT_SEC}s -- "
                "anything not yet shipped is lost if this process exits now.",
                stacklevel=3,
            )


def _start_shipper[T](
    name: str, ship: Callable[[list[T]], None], *, q_size: int, flush_size: int
) -> _Shipper[T]:
    queue: Queue[T | None] = SPAWN_CONTEXT.Queue(maxsize=q_size)
    ready, flushed = SPAWN_CONTEXT.Event(), SPAWN_CONTEXT.Event()
    process = SPAWN_CONTEXT.Process(
        target=ship_batches,
        args=(queue, ship, LogProcParams(flush_size=flush_size, ready=ready, flushed=flushed)),
        name=f"dltrack-{name}",
        daemon=True,
    )
    process.start()
    return _Shipper(name, queue, process, ready, flushed)


class DLTrackLoggerSettings(BaseModel, frozen=True, extra="forbid"):
    """Tunables for the logger's background metric/artifact-shipping queues."""

    metrics_q_size: int = 500
    """Max metric batches buffered before `log_metrics` starts warning about backpressure."""
    metrics_q_flush_size: int = 100
    """Metric batches accumulated before the background process ships them to the server."""
    artifact_q_size: int = 100
    """Max artifact batches buffered before `log_artifact` starts warning about backpressure."""
    artifact_q_flush_size: int = 10
    """Artifact batches accumulated before the background process ships them to the server."""


class DLTrackLogger(Logger):
    """The lightning logger for dltrack."""

    def __init__(
        self,
        project_id: int,
        experiment_id: int | None = None,
        server_url: str = DEFAULT_SERVER_URL,
        settings: DLTrackLoggerSettings | None = None,
    ) -> None:
        """Initialize with an existing project/experiment id; an experiment is created if none is given."""
        settings = settings or DLTrackLoggerSettings()
        self._api = BasicDltrackAPI(base_url=server_url)
        if experiment_id is None:
            experiment = self._api.create_experiment(
                models.NewExperiment(project_id=project_id, source=models.ExperimentSource.PYTORCH_LIGHTNING)
            )
            experiment_id = experiment.id
        self._experiment_id = experiment_id
        self._run_id = self._api.create_run(models.NewRun(experiment_id=self._experiment_id)).id
        startup_start = time.perf_counter()
        self._metrics = _start_shipper(
            "metrics",
            self._api.log_metric_batch,
            q_size=settings.metrics_q_size,
            flush_size=settings.metrics_q_flush_size,
        )
        self._artifacts = _start_shipper(
            "artifacts",
            functools.partial(_ship_artifacts, self._api, self._experiment_id, self._run_id),
            q_size=settings.artifact_q_size,
            flush_size=settings.artifact_q_flush_size,
        )
        for shipper in (self._metrics, self._artifacts):
            shipper.ready.wait(_STARTUP_WAIT_TIMEOUT_SEC)
        warn_if_startup_was_slow(time.perf_counter() - startup_start)
        super().__init__()

    @classmethod
    def from_names(
        cls,
        project_name: str,
        experiment_name: str = DEFAULT_EXPERIMENT_NAME,
        project_description: str = "",
        server_url: str = DEFAULT_SERVER_URL,
        settings: DLTrackLoggerSettings | None = None,
    ) -> DLTrackLogger:
        """
        Initialize by project/experiment name instead of raw ids, creating either that don't exist yet.

        `project_description` only applies the first time `project_name` is seen -- once a project
        exists, later calls just reuse it as-is.
        """
        api = BasicDltrackAPI(base_url=server_url)
        project = api.get_or_create_project(project_name, description=project_description)
        experiment = api.get_or_create_experiment(
            project.id, name=experiment_name, source=models.ExperimentSource.PYTORCH_LIGHTNING
        )
        return cls(
            project_id=project.id, experiment_id=experiment.id, server_url=server_url, settings=settings
        )

    @property
    @override
    def name(self) -> str:
        return "dltrack-logger"

    @property
    @override
    def version(self) -> int | str | None:
        return 1

    @override
    def log_hyperparams(self, params: dict[str, Any] | Namespace, *args: Any, **kwargs: Any) -> None:
        """
        Log hyperparameters.

        Values are stored and later displayed exactly as given -- dltrack never guesses at or
        rewrites a value's type. A `Namespace` built from `argparse` without a `type=` on
        `add_argument` hands every value over as a string (e.g. `"128"`, not `128`); that's the
        usual cause of a hyperparameter column that looks numeric but sorts/compares as text.
        """
        if isinstance(params, Namespace):
            params = vars(params)
        self._api.log_hyperparams(
            models.NewHyperParams.from_raw(
                run_id=self._run_id,
                experiment_id=self._experiment_id,
                hparams=params,
            )
        )

    @override
    def log_metrics(self, metrics: dict[str, float], step: int | None = None) -> None:
        """
        Validate and timestamp `metrics` right here, at the call site, then queue them for shipping.

        Validating here (not in the shipping process) raises a bad metric at the line that logged
        it, instead of the server rejecting a whole batch of otherwise-good metrics later on.
        """
        if step is None:
            msg = "dltrack needs an explicit step for every metric (Lightning always passes one)"
            raise ValueError(msg)
        self._metrics.put(
            models.LoggedMetrics(
                metrics=metrics,  # pyright: ignore[reportArgumentType]
                step=step,
                experiment_id=self._experiment_id,
                run_id=self._run_id,
                timestamp_utc=pendulum.now(pendulum.UTC),
            )
        )

    def log_artifact(self, artifacts: Sequence[AnyArtifact]) -> None:
        """Queue `artifacts` for encoding and upload."""
        self._artifacts.put(artifacts)

    @override
    def finalize(self, status: str) -> None:
        """
        Block until everything logged so far has actually reached the server.

        Lightning calls this at the end of every `fit`/`test`/...; the shipping processes are
        daemons, so a script that exits right after (the usual case) would otherwise kill them with
        up to `_SHIP_INTERVAL_SEC` of logged-but-unshipped metrics and artifacts still queued.
        """
        for shipper in (self._metrics, self._artifacts):
            shipper.flush()
        super().finalize(status)
