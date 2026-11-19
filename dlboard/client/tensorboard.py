"""
Bring a TensorBoard logdir into dlboard, and keep bringing what its runs go on to write (#702).

The logdir is one experiment, and each directory under it that holds event files is a run, named by its path in the
logdir. Scalars become metrics, histograms histograms, images image artifacts, and the hparams plugin's values the run's
hyperparameters. Every other kind of summary (text, audio, PR curves, ...) is left out and counted in the report.

It goes through the REST API like any run, in batches, so a client install can import into a server anywhere. It is
incremental: how far it read each event file (a byte offset) is kept in a state file, by default `.dlboard-import.json`
in the logdir, so importing again brings only what is new, and doing that on an interval follows runs still writing. An
event file cut short (a run killed mid-write) is read up to its last whole record, and the rest is read once it is there.
"""

from __future__ import annotations

import collections
import contextlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pendulum
from pydantic import BaseModel, Field

from dlboard import models
from dlboard.client import _tfevents

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dlboard.client._rest_api import BasicDlboardAPI

STATE_FILE: Final = ".dlboard-import.json"
"""Where an import keeps how far it read, in the logdir, unless told otherwise."""

_EVENT_FILE_MARK: Final = "tfevents"
_BATCH: Final = 500
"""How many steps' metrics, or artifacts, one request carries."""


class _RunState(BaseModel, extra="forbid"):
    run_id: int
    read: dict[str, int] = Field(default_factory=dict)
    """How far each of the run's event files was read, in bytes, by file name."""


class _ImportState(BaseModel, extra="forbid"):
    experiment_id: int
    runs: dict[str, _RunState] = Field(default_factory=dict)
    """By the run's directory, relative to the logdir."""


class _StateFile(BaseModel, extra="forbid"):
    imports: dict[str, _ImportState] = Field(default_factory=dict)
    """By the server and the experiment imported into: the same logdir can go to two servers."""


@dataclass
class ImportReport:
    """What an import brought across, and what it left out."""

    runs: int = 0
    """Runs that had something new."""
    scalars: int = 0
    histograms: int = 0
    images: int = 0
    hparams: int = 0
    unread: collections.Counter[str] = field(default_factory=collections.Counter)
    """Summaries of a kind dlboard does not import, by the TensorBoard plugin that wrote them."""

    def __str__(self) -> str:
        """One line of it, for the command to print."""
        left_out = ", ".join(f"{count} {plugin}" for plugin, count in sorted(self.unread.items()))
        return (
            f"{self.runs} runs: {self.scalars} scalars, {self.histograms} histograms, {self.images} images, "
            f"{self.hparams} sets of hyperparameters" + (f"; left out {left_out}" if left_out else "")
        )


def import_logdir(
    api: BasicDlboardAPI,
    logdir: Path,
    *,
    project: str,
    experiment: str | None = None,
    state_path: Path | None = None,
) -> ImportReport:
    """
    Bring what is new in `logdir` into `project` (the experiment named `experiment`, or after the logdir), and say what.

    Safe to run again, and again while its runs are still writing: each pass reads only what the last one did not.
    """
    state_path = state_path or logdir / STATE_FILE
    states = _StateFile.model_validate_json(state_path.read_text()) if state_path.exists() else _StateFile()
    name = experiment or logdir.resolve().name
    key = f"{api.base_url} {project}/{name}"
    if key not in states.imports:
        project_id = api.get_or_create_project(project).id
        found = api.get_or_create_experiment(project_id, name, source=models.ExperimentSource.TENSORBOARD)
        states.imports[key] = _ImportState(experiment_id=found.id)
    imported = states.imports[key]
    report = ImportReport()
    with tempfile.TemporaryDirectory() as scratch:
        for run_dir, files in _runs(logdir):
            if run_dir not in imported.runs:
                created = api.create_run(models.NewRun(experiment_id=imported.experiment_id, name=run_dir))
                imported.runs[run_dir] = _RunState(run_id=created.id)
            run = _Importing(
                api, imported.experiment_id, imported.runs[run_dir].run_id, Path(scratch), report
            )
            for file in files:
                read = imported.runs[run_dir].read
                read[file.name] = run.read(file, read.get(file.name, 0))
                state_path.write_text(states.model_dump_json(indent=2))
            run.finish()
    return report


def _runs(logdir: Path) -> Iterator[tuple[str, list[Path]]]:
    """Each directory of the logdir that holds event files, as a run's name, with its files in the order they were written."""
    by_dir: dict[Path, list[Path]] = collections.defaultdict(list)
    for file in logdir.rglob(f"*{_EVENT_FILE_MARK}*"):
        if file.is_file():
            by_dir[file.parent].append(file)
    for directory in sorted(by_dir):
        relative = directory.relative_to(logdir).as_posix()
        yield (logdir.resolve().name if relative == "." else relative), sorted(by_dir[directory])


@dataclass
class _Step:
    """What a run logged at one step, gathered from every event of it: when the first said it was, and its values."""

    wall_time: float
    metrics: dict[str, float | None] = field(default_factory=dict)
    histograms: dict[str, models.Histogram] = field(default_factory=dict)


class _Importing:
    """One run's import: what it reads, gathered into batches, sent as they fill."""

    def __init__(
        self, api: BasicDlboardAPI, experiment_id: int, run_id: int, scratch: Path, report: ImportReport
    ) -> None:
        self._api, self._experiment_id, self._run_id = api, experiment_id, run_id
        self._scratch, self._report = scratch, report
        self._steps: dict[int, _Step] = {}
        self._images: list[tuple[models.NewArtifact, Path]] = []
        self._last: float | None = None

    def read(self, file: Path, offset: int) -> int:
        """Send what `file` holds past `offset`, and how far that is: the next import starts there."""
        for end, record in _tfevents.records(file, offset):
            event = _tfevents.event(record)
            for summary in event.summaries:
                self._take(event, summary)
            if event.summaries:
                self._last = event.wall_time
            offset = end
            if len(self._steps) >= _BATCH or len(self._images) >= _BATCH:
                self._send()
        self._send()
        return offset

    def finish(self) -> None:
        """Say the run ended when it last wrote, if this pass found anything: a run that writes again is finished again."""
        if self._last is not None:
            self._report.runs += 1
            ended = pendulum.from_timestamp(self._last, tz="UTC")
            self._api.finish_run(self._run_id, models.RunStatus.FINISHED, ended_at=ended)

    def _take(self, event: _tfevents.Event, summary: _tfevents.Summary) -> None:
        match summary:
            case _tfevents.Scalar(tag=tag, value=value):
                self._at(event).metrics[tag] = value
                self._report.scalars += 1
            case _tfevents.Histo(tag=tag, histogram=histogram):
                self._at(event).histograms[tag] = histogram
                self._report.histograms += 1
            case _tfevents.Picture(tag=tag, png=png):
                path = self._scratch / f"{len(self._images)}-{event.step}.png"
                path.write_bytes(png)
                artifact = models.NewArtifact(
                    key=tag,
                    fname=f"{event.step}.png",
                    run_id=self._run_id,
                    experiment_id=self._experiment_id,
                    step=event.step,
                    created_at=pendulum.from_timestamp(event.wall_time, tz="UTC"),
                )
                self._images.append((artifact, path))
                self._report.images += 1
            case _tfevents.Hparams(values=values):
                self._api.log_hyperparams(
                    models.NewHyperParams.from_raw(self._run_id, self._experiment_id, values)
                )
                self._report.hparams += 1
            case _tfevents.Unread(plugin=plugin):
                self._report.unread[plugin] += 1

    def _at(self, event: _tfevents.Event) -> _Step:
        """What was logged at the event's step, gathered across the events that logged at it."""
        return self._steps.setdefault(event.step, _Step(event.wall_time))

    def _send(self) -> None:
        if self._steps:
            self._api.log_metric_batch(
                [
                    models.LoggedMetrics(
                        step=step,
                        metrics=at.metrics,
                        histograms=at.histograms,
                        experiment_id=self._experiment_id,
                        run_id=self._run_id,
                        timestamp_utc=pendulum.from_timestamp(at.wall_time, tz="UTC"),
                    )
                    for step, at in self._steps.items()
                ]
            )
            self._steps = {}
        if self._images:
            self._api.log_artifact_batch(self._images)
            for _, path in self._images:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
            self._images = []
