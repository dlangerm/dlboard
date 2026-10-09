"""The pytorch lightning logger for use with dlboard."""

from __future__ import annotations

import atexit
import contextlib
import functools
import json
import shutil
import tempfile
import time
import warnings
from argparse import Namespace
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from queue import Full
from typing import TYPE_CHECKING, Any, Final, Generic, Literal, TypeVar, cast

import numpy as np
import pendulum
import requests
from pydantic import BaseModel
from pydantic_core import to_jsonable_python
from pydantic_settings import BaseSettings
from pytorch_lightning.loggers import Logger
from pytorch_lightning.loggers.utilities import (
    _scan_checkpoints,  # pyright: ignore[reportPrivateUsage, reportUnknownVariableType]
)
from pytorch_lightning.utilities import rank_zero_only
from typing_extensions import override

from dlboard import models
from dlboard._batching import BatchParams, ship_batches
from dlboard._mp_context import SPAWN_CONTEXT
from dlboard.client._rest_api import DEFAULT_SERVER_URL, BasicDlboardAPI
from dlboard.client.artifacts.figure import Figure
from dlboard.client.artifacts.file import File, FileKind

DEFAULT_EXPERIMENT_NAME = "default"

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from multiprocessing import Queue
    from multiprocessing.context import SpawnProcess
    from multiprocessing.synchronize import Event as MPEvent

    from pytorch_lightning.callbacks import ModelCheckpoint

    from dlboard.client.artifacts.figure import SavesFigure
    from dlboard.models._artifact import AnyArtifact


_STARTUP_WARN_THRESHOLD_SEC = 2.0
"""How long a worker process's import + startup can take before we warn it's the likely cause of
any backpressure warnings seen right after training starts, rather than dlboard itself."""
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
            f"dlboard's logging worker processes took {elapsed_sec:.1f}s to start up. This is "
            "almost always import overhead from your training script or its other dependencies, not "
            "dlboard -- any backpressure warnings logged in the next few steps are a side effect of "
            "this startup delay rather than an ongoing throughput problem.",
            stacklevel=2,
        )


def is_rejection(exc: Exception) -> bool:
    """
    Whether the server refused a batch outright (4xx) -- resending the exact same batch can never succeed.

    A connection error or 5xx is transient instead, as are a 408/429, which only ask to retry later.
    """
    if not isinstance(exc, requests.HTTPError) or exc.response is None:
        return False
    status = exc.response.status_code
    return HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR and (
        status not in _TRANSIENT_CLIENT_ERRORS
    )


def is_oversized(exc: Exception) -> bool:
    """
    Whether the server rejected a batch for being too large (413) -- see `ship_batches`'s `is_oversized`.

    Only reachable once recovering from an outage has let a backlog grow past
    `DLBOARD_MAX_UPLOAD_MB` on the server; an ordinary batch (`metrics_q_flush_size`/
    `artifact_q_flush_size` items) is nowhere near that limit on its own.
    """
    return (
        isinstance(exc, requests.HTTPError)
        and exc.response is not None
        and exc.response.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    )


def _ship_artifacts(
    api: BasicDlboardAPI, experiment_id: int, run_id: int, batch: list[Sequence[AnyArtifact]]
) -> None:
    """Encode `batch` into a scratch dir that's deleted once it's shipped, then ship it."""
    with tempfile.TemporaryDirectory() as tmp:
        pairs = [
            artifact.to_artifact(Path(tmp), run_id=run_id, experiment_id=experiment_id)
            for artifacts in batch
            for artifact in artifacts
        ]
        api.log_artifact_batch(pairs)


T = TypeVar("T")
"""Pre-3.12 `TypeVar` style (the client's floor is 3.10): the kind of item a shipper moves."""

_DROP_WARNING_INTERVAL_SEC = 5.0
"""Rate-limits `_Shipper.put`'s "dropping items" warning while a queue stays full, instead of one
warning per dropped item -- which, at training-loop frequency, would itself flood the console."""


@dataclass
class _Shipper(Generic[T]):
    """One background process running `ship_batches`, plus the queue feeding it."""

    name: str
    queue: Queue[T | None]
    process: SpawnProcess
    ready: MPEvent
    flushed: MPEvent
    _dropped_total: int = field(default=0, init=False)
    _dropped_since_warning: int = field(default=0, init=False)
    _last_drop_warning: float = field(default=0.0, init=False)

    def put(self, item: T) -> None:
        """
        Queue `item` for shipping, never blocking: training must never stall on dlboard falling behind.

        A full queue means the shipping process can't keep up, or the server/network is down --
        rather than block (which could wedge the training loop indefinitely once the queue filled),
        this drops `item` and counts it, warning at most once every `_DROP_WARNING_INTERVAL_SEC`
        while it keeps happening, plus a final total once `finalize()` calls `flush()`.
        """
        if not self.process.is_alive():
            msg = f"dlboard's {self.name} shipping process died, refusing to back up its queue"
            raise RuntimeError(msg)
        try:
            self.queue.put_nowait(item)
        except Full:
            self._dropped_total += 1
            self._dropped_since_warning += 1
            now = time.monotonic()
            if now - self._last_drop_warning > _DROP_WARNING_INTERVAL_SEC:
                warnings.warn(
                    f"dlboard's {self.name} queue is full -- the shipping process can't keep up, or the "
                    f"server/network is down. Dropped {self._dropped_since_warning} item(s) rather than "
                    "block training; see `DLBoardLoggerSettings` to size the queue, or check the server.",
                    stacklevel=3,
                )
                self._dropped_since_warning = 0
                self._last_drop_warning = now

    def flush(self) -> None:
        """Block until everything queued so far has been shipped (or rejected by the server)."""
        if self._dropped_total:
            warnings.warn(
                f"dlboard dropped {self._dropped_total} {self.name} item(s) during this run because its "
                "queue was full -- see the warnings above for when.",
                stacklevel=3,
            )
            self._dropped_total = 0
        if not self.process.is_alive():
            warnings.warn(
                f"dlboard's {self.name} shipping process is dead, nothing queued can be flushed",
                stacklevel=3,
            )
            return
        self.flushed.clear()
        self.queue.put(None)
        if not self.flushed.wait(_FLUSH_TIMEOUT_SEC):
            warnings.warn(
                f"dlboard's {self.name} still hadn't all been shipped after {_FLUSH_TIMEOUT_SEC}s -- "
                "anything not yet shipped is lost if this process exits now.",
                stacklevel=3,
            )


def _start_shipper(
    name: str, ship: Callable[[list[T]], None], *, q_size: int, flush_size: int
) -> _Shipper[T]:
    queue: Queue[T | None] = SPAWN_CONTEXT.Queue(maxsize=q_size)
    ready, flushed = SPAWN_CONTEXT.Event(), SPAWN_CONTEXT.Event()
    process = SPAWN_CONTEXT.Process(
        target=ship_batches,
        args=(queue, ship, BatchParams(flush_size, _SHIP_INTERVAL_SEC, ready, flushed)),
        kwargs={"is_permanent": is_rejection, "is_oversized": is_oversized},
        name=f"dlboard-{name}",
        daemon=True,
    )
    process.start()
    return _Shipper(name, queue, process, ready, flushed)


class DLBoardLoggerSettings(BaseModel, frozen=True, extra="forbid"):
    """Tunables for the logger's background metric/artifact-shipping queues."""

    metrics_q_size: int = 500
    """Max metric batches buffered before `log_metrics` starts warning about backpressure."""
    metrics_q_flush_size: int = 100
    """Metric batches accumulated before the background process ships them to the server."""
    artifact_q_size: int = 100
    """Max artifact batches buffered before `log_artifact` starts warning about backpressure."""
    artifact_q_flush_size: int = 10
    """Artifact batches accumulated before the background process ships them to the server."""


class _LoggerEnv(BaseSettings):
    """Env vars the logger reads when the matching argument isn't passed."""

    dlboard_run_id: int | None = None
    """An existing run to attach to instead of creating one -- see `DLBoardLogger`'s `run_id`."""


def _jsonable_fallback(value: object) -> object:
    """What pydantic's JSON serializer can't convert itself: numpy scalars, `Namespace`s, anything else as text."""
    match value:
        case np.generic():
            return cast("object", value.item())
        case Namespace():
            return vars(value)
        case _:
            return str(value)


def _flatten_json(value: object, prefix: str) -> dict[str, models.ValidJsonTypes]:
    """Join nested dict keys with `"/"`, and keep a list as one JSON string -- a hparam is a single cell."""
    match value:
        case dict():
            return {
                key: leaf
                for name, child in cast("dict[str, object]", value).items()
                for key, leaf in _flatten_json(child, f"{prefix}/{name}" if prefix else name).items()
            }
        case list():
            return {prefix: json.dumps(value)}
        case str() | int() | float() | bool() | None:
            return {prefix: value}
        case _:
            return {prefix: str(value)}


def flatten_hparams(params: dict[str, Any] | Namespace) -> models.FlatHparamDict:
    """
    Turn `params` into the flat `{"a/b": scalar}` dict a hyperparameter blob stores, like Lightning's loggers do.

    Pydantic does the serializing, so a `BaseModel`, a pydantic dataclass (and its own aliases and
    field serializers), a stdlib dataclass, a `Namespace`, an enum or a tuple all just work, nested
    arbitrarily. Nested keys are joined with `"/"`, which is also how the experiment page groups
    them. A list stays one value (as a JSON string). Scalars pass through untouched.
    """
    return _flatten_json(to_jsonable_python(params, fallback=_jsonable_fallback), "")


def _is_rank_zero() -> bool:
    """
    Whether this is global rank 0, the same notion `rank_zero_only` uses.

    Lightning sets `.rank` on it from the launcher's env vars at import, but doesn't declare it for
    type checkers.
    """
    return getattr(rank_zero_only, "rank", 0) == 0


_UNRESOLVED_ID: Final = 0
"""Stands in for an id a non-zero rank never resolves (it makes no server calls, see `DLBoardLogger`)."""


class DLBoardLogger(Logger):
    """
    The lightning logger for dlboard.

    Only global rank 0 logs, like Lightning's own loggers: under DDP every rank builds a logger and
    Lightning calls every one of them, but a non-zero rank here makes no server calls, starts no
    shipping processes and drops whatever it's asked to log. A single run is therefore created per
    job, not per GPU.
    """

    _experiment_id: int
    _run_id: int
    """Both resolved on rank 0 only -- every method that reads them is `rank_zero_only`."""
    _last_step = 0
    """The step of the latest logged metrics, which a checkpoint saved right after is stamped with."""

    def __init__(  # noqa: PLR0913
        self,
        project_id: int,
        experiment_id: int | None = None,
        server_url: str = DEFAULT_SERVER_URL,
        settings: DLBoardLoggerSettings | None = None,
        run_id: int | None = None,
        *,
        log_model: bool | Literal["all"] = False,
    ) -> None:
        """
        Initialize with an existing project/experiment id; an experiment is created if none is given.

        A run is created too, unless `run_id` (default: the `DLBOARD_RUN_ID` env var) names an
        existing one to attach to -- for resuming a run, or sharing one between processes that were
        launched separately. `experiment_id` may be omitted then (it's the run's own); a different
        one is an error.

        `log_model` is as for Lightning's MLflow/W&B loggers, for a `ModelCheckpoint` callback:
        `"all"` uploads every checkpoint as it is saved (or `True`, when `save_top_k=-1`), while `True`
        otherwise uploads only the ones still kept (best/last) once training finishes. Each is a
        `File` of kind `FileKind.CHECKPOINT`, keyed `checkpoints/<file name>`.
        """
        super().__init__()
        run_id = run_id if run_id is not None else _LoggerEnv().dlboard_run_id
        if not _is_rank_zero():
            if run_id is not None:
                self._run_id = run_id
            return
        settings = settings or DLBoardLoggerSettings()
        self._api = BasicDlboardAPI(base_url=server_url)
        # Up front, before any worker starts: the workers treat every 4xx as "drop this batch" (see
        # `is_rejection`), so bad credentials would otherwise only show up as silently lost metrics.
        self._api.whoami()
        if run_id is not None:
            run = self._api.get_run(run_id)
            if experiment_id not in (None, run.experiment_id):
                msg = f"run {run_id} belongs to experiment {run.experiment_id}, not {experiment_id}"
                raise ValueError(msg)
            experiment_id = run.experiment_id
        elif experiment_id is None:
            experiment_id = self._api.create_experiment(
                models.NewExperiment(project_id=project_id, source=models.ExperimentSource.PYTORCH_LIGHTNING)
            ).id
        self._experiment_id = experiment_id
        self._run_id = (
            run_id
            if run_id is not None
            else self._api.create_run(models.NewRun(experiment_id=experiment_id)).id
        )
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
        self._unflushed = False
        self._log_model = log_model
        self._checkpoint_callback: ModelCheckpoint | None = None
        self._logged_checkpoint_mtimes: dict[str, float] = {}
        self._checkpoint_copy_dirs: list[Path] = []
        # The shipping processes are daemons -- killed with whatever's still queued the moment the
        # interpreter exits, unless something flushes them first. Lightning's trainer calls
        # `finalize()` after `fit`/`test`/..., but a crash, or any script that drives this logger
        # without going through a `Trainer` at all, never would otherwise.
        atexit.register(self._finalize_at_exit)
        super().__init__()

    @classmethod
    def from_names(  # noqa: PLR0913
        cls,
        project_name: str,
        experiment_name: str = DEFAULT_EXPERIMENT_NAME,
        project_description: str = "",
        server_url: str = DEFAULT_SERVER_URL,
        settings: DLBoardLoggerSettings | None = None,
        run_id: int | None = None,
        *,
        log_model: bool | Literal["all"] = False,
    ) -> DLBoardLogger:
        """
        Initialize by project/experiment name instead of raw ids, creating either that don't exist yet.

        `project_description` only applies the first time `project_name` is seen -- once a project
        exists, later calls just reuse it as-is. `run_id` and `log_model` are as for `__init__`.
        """
        if not _is_rank_zero():
            # Every rank runs this, and a get-or-create isn't safe to race: two ranks asking for the
            # same new experiment at once could each create it. Rank 0 alone resolves the names.
            return cls(
                project_id=_UNRESOLVED_ID,
                server_url=server_url,
                settings=settings,
                run_id=run_id,
                log_model=log_model,
            )
        api = BasicDlboardAPI(base_url=server_url)
        api.whoami()
        project = api.get_or_create_project(project_name, description=project_description)
        experiment = api.get_or_create_experiment(
            project.id, name=experiment_name, source=models.ExperimentSource.PYTORCH_LIGHTNING
        )
        return cls(
            project_id=project.id,
            experiment_id=experiment.id,
            server_url=server_url,
            settings=settings,
            run_id=run_id,
            log_model=log_model,
        )

    @property
    def run_id(self) -> int | None:
        """The run this logger writes to -- hand it to other processes' `run_id=` to share the run."""
        return getattr(self, "_run_id", None)

    @property
    @override
    def name(self) -> str:
        return "dlboard-logger"

    @property
    @override
    def version(self) -> int | str | None:
        return 1

    @override
    @rank_zero_only
    def log_hyperparams(self, params: dict[str, Any] | Namespace, *args: Any, **kwargs: Any) -> None:
        """
        Log hyperparameters.

        Nested configs (pydantic models and dataclasses, stdlib dataclasses, dicts, ...) are flattened
        to `"parent/child"` keys -- see `flatten_hparams`. Scalar values are stored and later displayed
        exactly as given -- dlboard never guesses at or rewrites a value's type. A `Namespace` built from `argparse` without a `type=` on
        `add_argument` hands every value over as a string (e.g. `"128"`, not `128`); that's the
        usual cause of a hyperparameter column that looks numeric but sorts/compares as text.
        """
        self._api.log_hyperparams(
            models.NewHyperParams.from_raw(
                run_id=self._run_id,
                experiment_id=self._experiment_id,
                hparams=flatten_hparams(params),
            )
        )

    @override
    @rank_zero_only
    def log_metrics(self, metrics: dict[str, float], step: int | None = None) -> None:
        """
        Validate and timestamp `metrics` right here, at the call site, then queue them for shipping.

        Validating here (not in the shipping process) raises a bad metric at the line that logged
        it, instead of the server rejecting a whole batch of otherwise-good metrics later on.
        """
        if step is None:
            msg = "dlboard needs an explicit step for every metric (Lightning always passes one)"
            raise ValueError(msg)
        self._last_step = step
        self._metrics.put(
            models.LoggedMetrics(
                metrics=metrics,  # pyright: ignore[reportArgumentType]
                step=step,
                experiment_id=self._experiment_id,
                run_id=self._run_id,
                timestamp_utc=pendulum.now(pendulum.UTC),
            )
        )
        self._unflushed = True

    @rank_zero_only
    def log_artifact(self, artifacts: Sequence[AnyArtifact]) -> None:
        """Queue `artifacts` for encoding and upload."""
        self._artifacts.put(artifacts)
        self._unflushed = True

    @rank_zero_only
    def log_figure(
        self,
        figure: SavesFigure,
        key: str,
        step: int,
        *,
        tags: dict[str, str] | None = None,
        save_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """
        Render `figure` to an image now and queue it as an artifact, like `mlflow.log_figure`.

        Rendered here, at the call site, so the figure can be closed or mutated straight after.
        The format is PNG unless `key` ends in another supported extension (`"loss.svg"`) or
        `save_kwargs` has a `format`; `key` itself is used as given. `save_kwargs` goes to
        `figure.savefig`.
        """
        self.log_artifact([Figure.from_figure(figure, key, step, tags=tags, save_kwargs=save_kwargs)])

    @override
    @rank_zero_only
    def after_save_checkpoint(self, checkpoint_callback: ModelCheckpoint) -> None:
        """Upload what `log_model` asks for: every checkpoint as it's saved, or just the kept ones at `finalize`."""
        if self._log_model == "all" or (self._log_model is True and checkpoint_callback.save_top_k == -1):
            self._upload_new_checkpoints(checkpoint_callback)
        elif self._log_model is True:
            self._checkpoint_callback = checkpoint_callback

    def _upload_new_checkpoints(self, checkpoint_callback: ModelCheckpoint) -> None:
        """Queue every checkpoint `checkpoint_callback` has saved or rewritten since the last call."""
        for mtime, path, score, tag in _scan_checkpoints(checkpoint_callback, self._logged_checkpoint_mtimes):
            # A copy: `save_top_k` can delete the original before the shipping process gets to read it.
            copy_dir = Path(tempfile.mkdtemp(prefix="dlboard-checkpoint-"))
            self._checkpoint_copy_dirs.append(copy_dir)
            copy = copy_dir / Path(path).name
            shutil.copy2(path, copy)
            self._logged_checkpoint_mtimes[path] = mtime
            # Lightning types `score` as a float, but it's `None` for a callback with no `monitor`.
            raw_score = cast("float | None", score)
            self.log_artifact(
                [
                    File(
                        key=f"checkpoints/{copy.name}",
                        path=copy,
                        kind=FileKind.CHECKPOINT,
                        step=self._last_step,
                        tags={"tag": tag}
                        | ({} if raw_score is None else {"score": f"{float(raw_score):.6g}"}),
                    )
                ]
            )

    @override
    @rank_zero_only
    def finalize(self, status: str) -> None:
        """
        Block until everything logged so far has actually reached the server.

        Lightning calls this at the end of every `fit`/`test`/...; the shipping processes are
        daemons, so a script that exits right after (the usual case) would otherwise kill them with
        up to `_SHIP_INTERVAL_SEC` of logged-but-unshipped metrics and artifacts still queued. Every call
        flushes, since Lightning calls this after each stage (`fit`, then `test`, ...) and what the
        later one logged is just as unshipped. Safe to call more than once -- `_Shipper.flush` itself
        tolerates a dead process, and warns about each dropped item only once.
        """
        if self._checkpoint_callback is not None:
            # Lightning hands over a weak proxy: once the trainer is gone there is nothing left to scan.
            with contextlib.suppress(ReferenceError):
                self._upload_new_checkpoints(self._checkpoint_callback)
        for shipper in (self._metrics, self._artifacts):
            shipper.flush()
        for copy_dir in self._checkpoint_copy_dirs:
            shutil.rmtree(copy_dir, ignore_errors=True)
        self._unflushed = False
        super().finalize(status)

    @rank_zero_only
    def _finalize_at_exit(self) -> None:
        """Catch a crash, or any training loop that drives this logger without a `Trainer` at all."""
        if self._unflushed:
            warnings.warn(
                "dlboard's logger was never finalized -- flushing whatever's still queued now, at "
                "interpreter exit. Call `trainer.logger.finalize('success')` yourself to avoid this "
                "delaying process exit.",
                stacklevel=2,
            )
            self.finalize("atexit")
