"""The pytorch lightning logger for use with dltrack."""

from __future__ import annotations

import contextlib
import logging
import tempfile
import time
import warnings
from argparse import Namespace
from pathlib import Path
from queue import Empty
from typing import TYPE_CHECKING, Any, NamedTuple, override

import pendulum
from pydantic import BaseModel
from pytorch_lightning.loggers import Logger

from dltrack import models
from dltrack._mp_context import SPAWN_CONTEXT
from dltrack.plugins.backend.basic_rest_backend import DEFAULT_SERVER_URL, BasicDltrackAPI

DEFAULT_EXPERIMENT_NAME = "default"

if TYPE_CHECKING:
    from collections.abc import Sequence
    from multiprocessing import Queue
    from multiprocessing.synchronize import Event as MPEvent

    from dltrack.models._artifact import AnyArtifact

    type QType = Queue[tuple[dict[str, float], int | None]]
    type ArtifactQType = Queue[Sequence[models.AnyArtifact]]


_log = logging.getLogger(__name__)

_STARTUP_WARN_THRESHOLD_SEC = 2.0
"""How long a worker process's import + startup can take before we warn it's the likely cause of
any backpressure warnings seen right after training starts, rather than dltrack itself."""
_STARTUP_WAIT_TIMEOUT_SEC = 60.0
"""Cap on how long `__init__` waits for a worker to report ready, so a wedged import can't hang forever."""


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
    wait_sec: float = 1


def process_metrics_async(
    exp_id: int,
    run_id: int,
    api: BasicDltrackAPI,
    q: QType,
    params: LogProcParams,
) -> None:
    """Logger background process."""
    params.ready.set()
    last_logged = time.perf_counter()
    cur_batch: list[models.LoggedMetrics] = []
    while True:
        try:
            step = -1
            metrics = {}
            with contextlib.suppress(Empty):
                metrics, step = q.get(timeout=params.wait_sec / 10)

            if metrics:
                obj = models.LoggedMetrics(
                    metrics=metrics,  # pyright: ignore[reportArgumentType]
                    step=step,
                    experiment_id=exp_id,
                    run_id=run_id,
                    timestamp_utc=pendulum.now(pendulum.UTC),
                )
                cur_batch.append(obj)
            log_diff = time.perf_counter() - last_logged
            if len(cur_batch) > params.flush_size or log_diff > params.wait_sec:
                _log.debug("logging batch of length %s", len(cur_batch))
                api.log_metric_batch(cur_batch)
                _log.info("Shipped len %s/%s metrics", len(cur_batch), q.qsize())
                cur_batch.clear()
                if log_diff > params.wait_sec:
                    _log.debug("Metrics logger process was idle")
                last_logged = time.perf_counter()
        except KeyboardInterrupt:
            raise
        except Exception:  # noqa: BLE001
            _log.exception("Failed to ship metrics")


def process_artifacts_async(
    exp_id: int,
    run_id: int,
    api: BasicDltrackAPI,
    q: ArtifactQType,
    params: LogProcParams,
) -> None:
    """Logger background process for artifacts."""
    params.ready.set()
    last_logged = time.perf_counter()
    cur_batch: list[models.NewArtifact] = []
    files: list[Path] = []
    with tempfile.TemporaryDirectory() as _tmp:
        tmp = Path(_tmp)
        while True:
            try:
                artifact_list = None
                with contextlib.suppress(Empty):
                    artifact_list = q.get(timeout=params.wait_sec / 10)

                if artifact_list:
                    _log.debug("popped %s off q len %s", len(artifact_list), q.qsize())
                    for art in artifact_list:
                        obj, target = art.to_artifact(tmp, run_id=run_id, experiment_id=exp_id)
                        cur_batch.append(obj)
                        files.append(target)

                log_diff = time.perf_counter() - last_logged
                if len(cur_batch) > params.flush_size or log_diff > params.wait_sec:
                    _log.debug("logging batch of artifacts length %s", len(cur_batch))
                    api.log_artifact_batch(cur_batch, files)
                    _log.info("shipped artifact batch of length %s/%s", len(files), q.qsize())
                    cur_batch.clear()
                    files.clear()
                    if log_diff > params.wait_sec:
                        _log.warning("Artifact logger process was idle")
                    last_logged = time.perf_counter()
            except KeyboardInterrupt:
                _log.info("keyboard interrupt detected")
                raise
            except Exception:  # noqa: BLE001
                _log.exception("Failed to ship artifacts")
                cur_batch.clear()
                files.clear()


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
        self._project_id = project_id
        self._api = BasicDltrackAPI(base_url=server_url)
        if experiment_id is None:
            experiment = self._api.create_experiment(
                models.NewExperiment(project_id=project_id, source=models.ExperimentSource.PYTORCH_LIGHTNING)
            )
            experiment_id = experiment.id
        self._experiment_id = experiment_id
        self._run_id = self._api.create_run(models.NewRun(experiment_id=self._experiment_id)).id
        self._metrics_q: QType = SPAWN_CONTEXT.Queue(maxsize=settings.metrics_q_size)
        self._art_q: ArtifactQType = SPAWN_CONTEXT.Queue(maxsize=settings.artifact_q_size)
        metrics_ready = SPAWN_CONTEXT.Event()
        art_ready = SPAWN_CONTEXT.Event()
        self._metric_proc = SPAWN_CONTEXT.Process(
            target=process_metrics_async,
            args=(
                self._experiment_id,
                self._run_id,
                self._api,
                self._metrics_q,
                LogProcParams(flush_size=settings.metrics_q_flush_size, ready=metrics_ready, wait_sec=5),
            ),
            name="logger-proc",
            daemon=True,
        )
        self._art_proc = SPAWN_CONTEXT.Process(
            target=process_artifacts_async,
            args=(
                self._experiment_id,
                self._run_id,
                self._api,
                self._art_q,
                LogProcParams(flush_size=settings.artifact_q_flush_size, ready=art_ready, wait_sec=5),
            ),
            name="artifact-proc",
            daemon=True,
        )
        startup_start = time.perf_counter()
        self._metric_proc.start()
        self._art_proc.start()
        metrics_ready.wait(_STARTUP_WAIT_TIMEOUT_SEC)
        art_ready.wait(_STARTUP_WAIT_TIMEOUT_SEC)
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
        assert self._metric_proc.is_alive(), "Metric process failed, refusing to back up queue"
        if self._metrics_q.full():
            warnings.warn(
                "Backpressure on metric queue detected, this will impact iteration speed", stacklevel=2
            )

        self._metrics_q.put((metrics, step))

    def log_artifact(self, artifacts: Sequence[AnyArtifact]) -> None:
        """Log an image."""
        assert self._metric_proc.is_alive(), "Artifact process failed, refusing to back up queue"
        if self._art_q.full():
            warnings.warn(
                "Backpressure on artifact queue detected, this will impact iteration speed", stacklevel=2
            )
        self._art_q.put(artifacts)
