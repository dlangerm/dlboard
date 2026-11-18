"""The PyTorch Lightning logger for dlboard: a Lightning `Logger` over a `dlboard.client.run.Run`."""

from __future__ import annotations

import atexit
import functools
import shutil
import tempfile
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from pytorch_lightning.loggers import Logger
from pytorch_lightning.loggers.utilities import (
    _scan_checkpoints,
)
from pytorch_lightning.utilities import rank_zero_only
from typing_extensions import override

from dlboard import models
from dlboard.client._rest_api import DEFAULT_SERVER_URL, BasicDlboardAPI
from dlboard.client._warnings import warn
from dlboard.client.artifacts.file import File
from dlboard.client.histograms import DEFAULT_BINNING
from dlboard.client.run import DEFAULT_EXPERIMENT_NAME, LaunchEnv, Run, RunSettings, call_if_alive
from dlboard.models import FileKind

if TYPE_CHECKING:
    from argparse import Namespace
    from collections.abc import Mapping, Sequence

    from pytorch_lightning.callbacks import ModelCheckpoint

    from dlboard.client._arrays import ArrayLike
    from dlboard.client.artifacts.figure import SavesFigure
    from dlboard.client.histograms import Binning
    from dlboard.models import Histogram, TagValue
    from dlboard.models._artifact import AnyArtifact

__all__ = ["DEFAULT_EXPERIMENT_NAME", "DLBoardLogger", "DLBoardLoggerSettings"]

DLBoardLoggerSettings = RunSettings
"""The logger's name for `RunSettings`, kept so existing scripts go on working."""


def _is_rank_zero() -> bool:
    """
    Whether this is global rank 0, the same notion `rank_zero_only` uses.

    Lightning sets `.rank` on it from the launcher's env vars at import, but doesn't declare it for
    type checkers.
    """
    return getattr(rank_zero_only, "rank", 0) == 0  # dlboard-ignore[forbid-string-ignore]


def _done_status(lightning_status: str, *, already_reported: bool) -> models.RunStatus | None:
    """
    What the status string Lightning hands `Logger.finalize` means for the run, or `None` if it means nothing.

    `"atexit"` is this logger's own flush when the script is ending without anyone having finalized it: it cannot
    tell a crash from a script that simply ended, so the outcome is unknown -- unless the run already reported one.
    """
    match lightning_status:
        case "success":
            return models.RunStatus.FINISHED
        case "failed":
            return models.RunStatus.FAILED
        case "atexit" if not already_reported:
            return models.RunStatus.UNKNOWN
        case _:
            return None


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

    _run: Run
    """Started on rank 0 only -- every method that reads it is `rank_zero_only`."""

    def __init__(  # noqa: PLR0913
        self,
        project_id: int,
        experiment_id: int | None = None,
        server_url: str = DEFAULT_SERVER_URL,
        settings: RunSettings | None = None,
        run_id: int | None = None,
        *,
        log_model: bool | Literal["all"] = False,
        pin: models.PinColor | None = None,
        keep: bool = False,
    ) -> None:
        """
        Initialize with an existing project/experiment id; an experiment is created if none is given.

        A run is created too, unless `run_id` (default: the `DLBOARD_RUN_ID` env var) names an
        existing one to attach to -- for resuming a run, or sharing one between processes that were
        launched separately. `experiment_id` may be omitted then (it's the run's own); a different
        one is an error.

        `pin` pins the run for you in that colour, and `keep` keeps it out of retention's reach (it takes an
        editor of the project), the moment it exists: a baseline or a release candidate need not wait for someone
        to click. A server that refuses fails this call, before anything starts logging.

        `log_model` is as for Lightning's MLflow/W&B loggers, for a `ModelCheckpoint` callback:
        `"all"` uploads every checkpoint as it is saved (or `True`, when `save_top_k=-1`), while `True`
        otherwise uploads only the ones still kept (best/last) once training finishes. Each is a
        `File` of kind `FileKind.CHECKPOINT`, keyed `checkpoints/<file name>`.
        """
        super().__init__()
        run_id = run_id if run_id is not None else LaunchEnv().dlboard_run_id
        if not _is_rank_zero():
            self._inert_run_id = run_id
            return
        self._run = Run.start(
            BasicDlboardAPI(base_url=server_url),
            project_id=project_id,
            experiment_id=experiment_id,
            run_id=run_id,
            source=models.ExperimentSource.PYTORCH_LIGHTNING,
            settings=settings,
            pin=pin,
            keep=keep,
        )
        self._log_model = log_model
        self._checkpoint_callbacks: dict[str, ModelCheckpoint] = {}
        self._logged_checkpoint_mtimes: dict[str, float] = {}
        self._checkpoint_copy_dirs: list[Path] = []
        # Lightning's trainer calls `finalize()` after `fit`/`test`/..., but a crash, or any script that drives this
        # logger without going through a `Trainer` at all, never would. The run's own exit hook would flush it, but not
        # upload the checkpoints `log_model` keeps: registered after it, this one runs first (`atexit` is LIFO).
        self._exit_hook = functools.partial(call_if_alive, weakref.WeakMethod(self._finalize_at_exit))
        atexit.register(self._exit_hook)

    @classmethod
    def from_names(  # noqa: PLR0913
        cls,
        project_name: str,
        experiment_name: str = DEFAULT_EXPERIMENT_NAME,
        project_description: str = "",
        server_url: str = DEFAULT_SERVER_URL,
        settings: RunSettings | None = None,
        run_id: int | None = None,
        *,
        log_model: bool | Literal["all"] = False,
        pin: models.PinColor | None = None,
        keep: bool = False,
    ) -> DLBoardLogger:
        """
        Initialize by project/experiment name instead of raw ids, creating either that don't exist yet.

        `project_description` only applies the first time `project_name` is seen -- once a project
        exists, later calls just reuse it as-is. `run_id`, `log_model`, `pin` and `keep` are as for `__init__`.
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
                pin=pin,
                keep=keep,
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
            pin=pin,
            keep=keep,
        )

    @property
    def run_id(self) -> int | None:
        """The run this logger writes to -- hand it to other processes' `run_id=` to share the run."""
        return self._run.run_id if hasattr(self, "_run") else self._inert_run_id

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
        """Log hyperparameters -- see `Run.log_hyperparams`."""
        self._run.log_hyperparams(params)

    @override
    @rank_zero_only
    def log_metrics(self, metrics: dict[str, float], step: int | None = None) -> None:
        """Validate, timestamp and queue `metrics` at the call site -- see `Run.log_metrics`."""
        if step is None:
            msg = "dlboard needs an explicit step for every metric (Lightning always passes one)"
            raise ValueError(msg)
        self._run.log_metrics(metrics, step)

    @rank_zero_only
    def log_histograms(
        self,
        histograms: Mapping[str, ArrayLike | Histogram],
        step: int,
        *,
        binning: Binning = DEFAULT_BINNING,
    ) -> None:
        """Bin `histograms` at the call site and queue the buckets -- see `Run.log_histograms`."""
        self._run.log_histograms(histograms, step, binning=binning)

    @rank_zero_only
    def log_artifact(self, artifacts: Sequence[AnyArtifact]) -> None:
        """Queue `artifacts` for encoding and upload -- see `Run.log_artifact`."""
        self._run.log_artifact(artifacts)

    @rank_zero_only
    def log_figure(
        self,
        figure: SavesFigure,
        key: str,
        step: int,
        *,
        tags: Mapping[str, TagValue] | None = None,
        save_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Render `figure` now and queue it as an artifact -- see `Run.log_figure`."""
        self._run.log_figure(figure, key, step, tags=tags, save_kwargs=save_kwargs)

    @override
    @rank_zero_only
    def after_save_checkpoint(self, checkpoint_callback: ModelCheckpoint) -> None:
        """Upload what `log_model` asks for: every checkpoint as it's saved, or just the kept ones at `finalize`."""
        if self._log_model == "all" or (self._log_model is True and checkpoint_callback.save_top_k == -1):
            self._upload_new_checkpoints(checkpoint_callback)
        elif self._log_model is True:
            # By `state_key`, which Lightning requires to be unique among a trainer's callbacks: more than
            # one `ModelCheckpoint` is allowed, and each one's kept checkpoints get uploaded.
            self._checkpoint_callbacks[checkpoint_callback.state_key] = checkpoint_callback

    def _upload_new_checkpoints(self, checkpoint_callback: ModelCheckpoint) -> None:
        """Queue every checkpoint `checkpoint_callback` has saved or rewritten since the last call."""
        for mtime, path, score, tag in _scan_checkpoints(checkpoint_callback, self._logged_checkpoint_mtimes):
            # A copy: `save_top_k` can delete the original before the shipping process gets to read it.
            copy_dir = Path(tempfile.mkdtemp(prefix="dlboard-checkpoint-"))
            self._checkpoint_copy_dirs.append(copy_dir)
            copy = copy_dir / Path(path).name
            shutil.copy2(path, copy)
            self._logged_checkpoint_mtimes[path] = mtime
            tags: dict[str, TagValue] = {"tag": tag}
            # Lightning types `score` as a float, but it's `None` for a callback with no `monitor`.
            if (raw_score := cast("float | None", score)) is not None:
                tags["score"] = float(raw_score)
            self._run.log_artifact(
                [
                    File(
                        key=f"checkpoints/{copy.name}",
                        path=copy,
                        kind=FileKind.CHECKPOINT,
                        # The step the callback saved this at, not the last one metrics were logged for: that
                        # can be the same for several saves, and a (key, step) pair is stored once.
                        step=checkpoint_callback._last_global_step_saved,  # noqa: SLF001
                        tags=tags,
                    )
                ]
            )

    @override
    @rank_zero_only
    def finalize(self, status: str) -> None:
        """
        Block until everything logged so far has actually reached the server, then say how the run ended.

        Lightning calls this at the end of every `fit`/`test`/...; the shipping processes are daemons, so a script
        that exits right after (the usual case) would otherwise kill them with logged-but-unshipped metrics and
        artifacts still queued. Every call flushes, since Lightning calls this after each stage (`fit`, then
        `test`, ...) and what the later one logged is just as unshipped. Safe to call more than once; bounded as
        `Run.flush` is, however long the server has been down.
        """
        for callback in self._checkpoint_callbacks.values():
            try:
                self._upload_new_checkpoints(callback)
            except ReferenceError:  # Lightning hands over a weak proxy, and the trainer is gone
                warn(
                    "dlboard could not upload the checkpoints `log_model` keeps: the trainer was gone by the time "
                    "this logger was finalized. Call `trainer.logger.finalize(...)` while the trainer is alive."
                )
        if self._run.flush():
            # Only once shipped: the shipping process reads each checkpoint from its copy.
            for copy_dir in self._checkpoint_copy_dirs:
                shutil.rmtree(copy_dir, ignore_errors=True)
            self._checkpoint_copy_dirs.clear()
        if (done := _done_status(status, already_reported=self._run.status is not None)) is not None:
            self._run.finish(done)
        super().finalize(status)

    @rank_zero_only
    def _finalize_at_exit(self) -> None:
        """Catch a crash, or any training loop that drives this logger without a `Trainer` at all."""
        if self._run.unflushed or self._run.status is None:
            warn(
                "dlboard's logger was never finalized -- flushing whatever's still queued now, at "
                "interpreter exit. Call `trainer.logger.finalize('success')` yourself to avoid this "
                "delaying process exit."
            )
            self.finalize("atexit")
