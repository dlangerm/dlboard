"""The pytorch lightning logger for use with dltrack."""

from __future__ import annotations

import contextlib
import logging
import time
from multiprocessing import Queue, get_context
from queue import Empty
from typing import TYPE_CHECKING, Any, override

import requests
from pytorch_lightning.loggers import Logger

from dltrack.client.api import DltrackAPI
from dltrack.models import LoggedMetrics, NewExperiment

if TYPE_CHECKING:
    from argparse import Namespace

QUEUE_SIZE = 500
FLUSH_SIZE = 100
MAX_WAIT_S = 5
_log = logging.getLogger(__name__)


def process(experiment_id: int, q: "Queue[tuple[dict[str, float], int | None]]") -> None:
    """Logger background process."""
    last_logged = time.perf_counter()
    cur_batch: list[LoggedMetrics] = []
    while True:
        try:
            step = -1
            metrics = {}
            with contextlib.suppress(Empty):
                metrics, step = q.get(timeout=1)
            if metrics:
                obj = LoggedMetrics(metrics=metrics, step=step, experiment_id=experiment_id)  # pyright: ignore[reportArgumentType]
                cur_batch.append(obj)
            log_diff = time.perf_counter() - last_logged
            if len(cur_batch) > FLUSH_SIZE or log_diff > MAX_WAIT_S:
                _log.debug("logging batch of length %s", len(cur_batch))
                res = requests.post(
                    url="http://localhost:8050/log-batch",
                    json=[batch_element.model_dump(mode="json") for batch_element in cur_batch],
                )
                res.raise_for_status()
                cur_batch.clear()
                if log_diff > MAX_WAIT_S:
                    _log.warning("Logger process was idle")
                last_logged = time.perf_counter()
        except Exception:  # noqa: BLE001
            _log.exception("Failed to ship metrics")


class DLTrackLogger(Logger):
    """The lightning logger for dltrack."""

    def __init__(self, project_id: int, experiment_id: int | None = None) -> None:
        """Initialize with an existing experiment id, if none is given one will be created."""
        self._project_id = project_id
        if experiment_id is None:
            experiment = DltrackAPI.create_experiment(NewExperiment(project_id=project_id))
            experiment_id = experiment.id
        self._experiment_id = experiment_id
        ctx = get_context("spawn")
        self._metrics_q: "Queue[tuple[dict[str, float], int | None]]" = ctx.Queue(maxsize=QUEUE_SIZE)

        self._proc = ctx.Process(
            target=process, args=(self._experiment_id, self._metrics_q), name="logger-proc", daemon=True
        )
        self._proc.start()
        super().__init__()

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
        print("hyperparameter logging not implemented")

    @override
    def log_metrics(self, metrics: dict[str, float], step: int | None = None) -> None:
        self._metrics_q.put((metrics, step))
