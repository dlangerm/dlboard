"""The pytorch lightning logger for use with dltrack."""

from __future__ import annotations

import contextlib
import json
import logging
import tempfile
import time
from argparse import Namespace
from multiprocessing import Queue, get_context
from pathlib import Path
from queue import Empty
from typing import TYPE_CHECKING, Annotated, Any, override

import dltype
import numpy as np  # noqa: TC002
import pendulum
import PIL
import PIL.Image
import torch
from pydantic import BaseModel, field_validator
from pytorch_lightning.loggers import Logger

from dltrack import models
from dltrack.client.api import DltrackAPI

if TYPE_CHECKING:
    type QType = Queue[tuple[dict[str, float], int | None]]
    type ArtifactQType = Queue[tuple[str, list[Image]]]

QUEUE_SIZE = 500
FLUSH_SIZE = 100
ARTIFACT_FLUSH_SIZE = 10
MAX_WAIT_S = 5
_log = logging.getLogger(__name__)


def process_metrics_async(exp_id: int, run_id: int, api: DltrackAPI, q: QType) -> None:
    """Logger background process."""
    last_logged = time.perf_counter()
    cur_batch: list[models.LoggedMetrics] = []
    while True:
        try:
            step = -1
            metrics = {}
            with contextlib.suppress(Empty):
                metrics, step = q.get(timeout=1)
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
            if len(cur_batch) > FLUSH_SIZE or log_diff > MAX_WAIT_S:
                _log.debug("logging batch of length %s", len(cur_batch))
                api.log_metric_batch(cur_batch)
                cur_batch.clear()
                if log_diff > MAX_WAIT_S:
                    _log.warning("Logger process was idle")
                last_logged = time.perf_counter()
        except Exception:  # noqa: BLE001
            _log.exception("Failed to ship metrics")


def process_artifacts_async(exp_id: int, run_id: int, api: DltrackAPI, q: ArtifactQType) -> None:
    """Logger background process for artifacts."""
    last_logged = time.perf_counter()
    cur_batch: list[models.NewArtifact] = []
    files: list[Path] = []
    with tempfile.TemporaryDirectory() as _tmp:
        tmp = Path(_tmp)
        while True:
            try:
                key = None
                img_list: list[Image] = []
                with contextlib.suppress(Empty):
                    key, img_list = q.get(timeout=1)

                if key and img_list:
                    for im in img_list:
                        im_underlying = im.image.numpy() if isinstance(im.image, torch.Tensor) else im.image
                        pil_img = PIL.Image.fromarray(im_underlying)
                        target = (tmp / str(len(files))).with_suffix(".jpg")
                        pil_img.save(target)

                        obj = models.NewArtifact(
                            key=key,
                            fname=target.name,
                            run_id=run_id,
                            experiment_id=exp_id,
                            step=im.step,
                            tags=im.tags,
                        )
                        cur_batch.append(obj)
                        files.append(target)

                log_diff = time.perf_counter() - last_logged
                if len(cur_batch) > ARTIFACT_FLUSH_SIZE or log_diff > MAX_WAIT_S:
                    _log.debug("logging batch of artifacts length %s", len(cur_batch))
                    api.log_artifact_batch(cur_batch, files)
                    cur_batch.clear()
                    files.clear()
                    if log_diff > MAX_WAIT_S:
                        _log.warning("Logger process was idle")
                    last_logged = time.perf_counter()
            except Exception:  # noqa: BLE001
                _log.exception("Failed to ship artifacts")


class DLTrackLogger(Logger):
    """The lightning logger for dltrack."""

    def __init__(
        self,
        project_id: int,
        experiment_id: int | None = None,
        server_url: str = "http://localhost:8050",
    ) -> None:
        """Initialize with an existing experiment id, if none is given one will be created."""
        self._project_id = project_id
        self._api = DltrackAPI(base_url=server_url)
        if experiment_id is None:
            experiment = self._api.create_experiment(models.NewExperiment(project_id=project_id))
            experiment_id = experiment.id
        self._experiment_id = experiment_id
        self._run_id = self._api.create_run(models.NewRun(experiment_id=self._experiment_id)).id
        ctx = get_context("spawn")
        self._metrics_q: QType = ctx.Queue(maxsize=QUEUE_SIZE)
        self._image_q: ArtifactQType = ctx.Queue(maxsize=QUEUE_SIZE)
        self._metric_proc = ctx.Process(
            target=process_metrics_async,
            args=(self._experiment_id, self._run_id, self._api, self._metrics_q),
            name="logger-proc",
            daemon=True,
        )
        self._img_proc = ctx.Process(
            target=process_artifacts_async,
            args=(self._experiment_id, self._run_id, self._api, self._image_q),
            name="artifact-proc",
            daemon=True,
        )
        self._metric_proc.start()
        self._img_proc.start()
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
        """Log hyperparameters."""
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
        self._metrics_q.put((metrics, step))

    def log_image(self, key: str, images: list[Image]) -> None:
        """Log an image."""
        self._image_q.put((key, images))


class Image(BaseModel, frozen=True, extra="forbid"):
    """An image to log to the backend."""

    image: Annotated[torch.Tensor | np.ndarray, dltype.UInt8Tensor("height width *channels")]
    """An image to log. Expected to be in CHW format."""
    tags: dict[str, str] = {}
    """Tags for the image, for use by plugins."""
    step: int
    """The global step of the trainer."""

    @field_validator("image", mode="before")
    @classmethod
    def _move_image_to_cpu(cls, image: torch.Tensor | np.ndarray) -> torch.Tensor | np.ndarray:
        if isinstance(image, torch.Tensor):
            return image.cpu()
        return image

    @field_validator("tags", mode="before")
    @classmethod
    def _str_to_json(cls, tags: str | dict[str, str]) -> dict[str, str]:
        if isinstance(tags, str):
            return json.loads(tags)
        return tags


Image.model_rebuild()
