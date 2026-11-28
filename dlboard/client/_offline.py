"""
Offline mode: a run logged to a folder instead of a server, and that folder brought to a server later (#703).

A run is offline when its server URL is a `file://` one, or `DLBOARD_OFFLINE_DIR` names a folder (`connect`). Then
`OfflineAPI` takes every call the run would have made of the server, and appends what it would have sent (the wire
payloads themselves) to the run's own folder, `run-<id>/` under the one named. Each process of the run (the script,
and its two shipping processes) appends to a segment file of its own, a JSON line a call, and an artifact's bytes are
copied beside them. The wire models' compatibility rules (`extra="ignore"`, fields only ever added) are what keep a
folder an older client wrote readable by a newer one.

`sync` replays a folder into a server through `BasicDlboardAPI`, as `dlboard sync` and `dlboard serve local --logdir`
do. It is incremental: how far it read each segment, and which run and experiment each local id became, are kept in a
state file in the folder, so syncing again sends only what is new, and an interrupted sync picks up where it stopped.
A line cut short (a run killed mid-write) is read once it is whole.
"""

from __future__ import annotations

import dataclasses
import os
import secrets
import shutil
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Union
from urllib.parse import unquote, urlsplit

import pendulum
from pydantic import AnyUrl, AwareDatetime, BaseModel, Field, TypeAdapter
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard import models
from dlboard._identity import resolve_username
from dlboard._wire import API_VERSION, FinishRun, GetOrCreateProject, Identity, MarkRun
from dlboard.client import _resumable
from dlboard.client._rest_api import BasicDlboardAPI

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from dlboard.client._rest_api import RunAPI

START_FILE: Final = "start.json"
"""What a run folder starts with: the project, experiment and run it was started as, by name."""
STATE_FILE: Final = ".dlboard-sync.json"
"""Where a sync keeps how far it read, in the folder, unless told otherwise."""
_FILES: Final = "files"
_RUN_PREFIX: Final = "run-"
_LOCAL_ID_BITS: Final = 48
"""Local ids are random below this: a collision between two runs of one folder is as likely as never."""


class OfflineSettings(BaseSettings):
    """Where a run is logged offline, from env vars: unset, it goes to its server."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    offline_dir: Path | None = None
    """A folder to log every run to instead of a server, to bring to one later with `dlboard sync` (or open with
    `dlboard serve local --logdir`)."""


def connect(server_url: str) -> RunAPI:
    """What a run talks to: the server at `server_url`, or a folder (a `file://` URL, or `DLBOARD_OFFLINE_DIR`)."""
    offline_dir = OfflineSettings().offline_dir
    if offline_dir is not None:
        return OfflineAPI(offline_dir)
    url = urlsplit(server_url)
    if url.scheme == "file":
        return OfflineAPI(Path(unquote(url.path)))
    return BasicDlboardAPI(base_url=server_url)


class _Start(BaseModel, frozen=True, extra="ignore"):
    project: GetOrCreateProject
    experiment: str | None
    """`None` for a new experiment of no name (`create_experiment`), which only this run's local id names."""
    source: models.ExperimentSource | None = None
    run: models.NewRun


class _Mark(BaseModel, frozen=True, extra="ignore"):
    call: Literal["mark"] = "mark"
    body: MarkRun


class _Hparams(BaseModel, frozen=True, extra="ignore"):
    call: Literal["hyperparams"] = "hyperparams"
    body: models.NewHyperParams


class _Context(BaseModel, frozen=True, extra="ignore"):
    call: Literal["context"] = "context"
    body: models.NewRunContext


class _Metrics(BaseModel, frozen=True, extra="ignore"):
    call: Literal["metrics"] = "metrics"
    body: list[models.LoggedMetrics]


class _Upload(BaseModel, frozen=True, extra="ignore"):
    call: Literal["upload"] = "upload"
    body: models.NewArtifact
    file: str
    """The bytes, in the run folder's `files/`."""


class _Link(BaseModel, frozen=True, extra="ignore"):
    call: Literal["link"] = "link"
    body: models.NewArtifact
    ref: AnyUrl


class _Finish(BaseModel, frozen=True, extra="ignore"):
    call: Literal["finish"] = "finish"
    body: FinishRun


_Record = Annotated[
    Union[_Mark, _Hparams, _Context, _Metrics, _Upload, _Link, _Finish], Field(discriminator="call")
]
_RECORD: TypeAdapter[_Record] = TypeAdapter(_Record)


def _local_id(*names: str) -> int:
    """A local id that the same names always make: the same project or experiment, however many runs start in it."""
    return zlib.crc32("\0".join(names).encode()) + 1


class OfflineAPI:
    """Every call a run makes of a server, written to `folder` instead (see the module): what it would have sent."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._projects: dict[int, GetOrCreateProject] = {}
        self._experiments: dict[int, tuple[int, str | None, models.ExperimentSource | None]] = {}

    def whoami(self) -> Identity:
        """Who would have logged this, and a check that the folder takes writes: a full disk fails here, not mid-run."""
        self.folder.mkdir(parents=True, exist_ok=True)
        probe = self.folder / f".probe-{os.getpid()}"
        probe.write_bytes(b"ok")
        probe.unlink()
        return Identity(id=0, username=resolve_username(), server_version="offline", api_version=API_VERSION)

    def get_or_create_project(self, name: str, description: str = "") -> models.Project:
        project_id = _local_id(name)
        self._projects[project_id] = GetOrCreateProject(name=name, description=description)
        return models.Project(id=project_id, name=name, description=description)

    def get_or_create_experiment(
        self, project_id: int, name: str = "default", source: models.ExperimentSource | None = None
    ) -> models.Experiment:
        experiment_id = _local_id(self._projects[project_id].name, name)
        self._experiments[experiment_id] = (project_id, name, source)
        return models.Experiment(id=experiment_id, project_id=project_id, name=name)

    def create_experiment(self, new_experiment: models.NewExperiment) -> models.Experiment:
        experiment_id = secrets.randbits(_LOCAL_ID_BITS)
        self._experiments[experiment_id] = (new_experiment.project_id, None, new_experiment.source)
        return models.Experiment(id=experiment_id, **new_experiment.model_dump())

    def create_run(self, run: models.NewRun) -> models.Run:
        """Start the run's folder with what it was started as, by name, for a sync to make it from."""
        run_id = secrets.randbits(_LOCAL_ID_BITS)
        project_id, experiment, source = self._experiments[run.experiment_id]
        start = _Start(project=self._projects[project_id], experiment=experiment, source=source, run=run)
        folder = self._run_folder(run_id)
        (folder / _FILES).mkdir(parents=True)
        partial = folder / f".{START_FILE}"
        partial.write_text(start.model_dump_json())
        partial.replace(folder / START_FILE)  # whole, or not there: a sync never reads half a start
        return models.Run(id=run_id, **run.model_dump())

    def get_run(self, run_id: int) -> models.Run:
        msg = f"run {run_id} is offline: an offline run cannot be attached to, only synced (`dlboard sync`) and resumed there"
        raise NotImplementedError(msg)

    def mark_run(
        self, run_id: int, *, pin: models.PinColor | None = None, keep: bool = False, tags: Sequence[str] = ()
    ) -> models.Run:
        self._append(run_id, _Mark(body=MarkRun(pin=pin, keep=keep, tags=list(tags))))
        return models.Run(id=run_id, experiment_id=0)

    def log_hyperparams(self, hyperparams: models.NewHyperParams) -> models.HyperParams:
        self._append(hyperparams.run_id, _Hparams(body=hyperparams))
        return models.HyperParams(id=0, **hyperparams.model_dump())

    def record_run_context(self, context: models.NewRunContext) -> models.RunContext:
        self._append(context.run_id, _Context(body=context))
        return models.RunContext(id=0, **context.model_dump())

    def log_metric_batch(self, metrics: list[models.LoggedMetrics]) -> None:
        if metrics:
            self._append(metrics[0].run_id, _Metrics(body=metrics))

    def log_artifact_batch(self, artifacts: Iterable[tuple[models.NewArtifact, Path | AnyUrl]]) -> None:
        for artifact, source in artifacts:
            match source:
                case Path():
                    kept = f"{secrets.token_hex(8)}{source.suffix}"
                    shutil.copyfile(source, self._run_folder(artifact.run_id) / _FILES / kept)
                    self._append(artifact.run_id, _Upload(body=artifact, file=kept))
                case AnyUrl():
                    self._append(artifact.run_id, _Link(body=artifact, ref=source))

    def finish_run(
        self, run_id: int, status: models.RunStatus, ended_at: AwareDatetime | None = None
    ) -> None:
        self._append(
            run_id, _Finish(body=FinishRun(status=status, ended_at=ended_at or pendulum.now(pendulum.UTC)))
        )

    def heartbeat(self, run_id: int) -> bool:
        """Nothing to tell: nobody gives an offline run up as lost. `False`, so a run stops saying it."""
        del run_id
        return False

    def _run_folder(self, run_id: int) -> Path:
        return self.folder / f"{_RUN_PREFIX}{run_id}"

    def _append(self, run_id: int, record: BaseModel) -> None:
        """One call, as one line of this process's own segment: two processes never write into one file."""
        with (self._run_folder(run_id) / f"{os.getpid()}.jsonl").open("a", encoding="utf-8") as segment:
            segment.write(record.model_dump_json() + "\n")


class _RunSync(BaseModel, extra="forbid"):
    run_id: int
    experiment_id: int
    read: dict[str, int] = Field(default_factory=dict)
    """How far each segment was read, in bytes, by file name."""


class _ServerSync(BaseModel, extra="forbid"):
    experiments: dict[int, int] = Field(default_factory=dict)
    """Which experiment each local one became."""
    runs: dict[str, _RunSync] = Field(default_factory=dict)
    """By the run's folder name."""


class _StateFile(BaseModel, extra="forbid"):
    servers: dict[str, _ServerSync] = Field(default_factory=dict)
    """By the server synced to: the same folder can go to two."""


@dataclasses.dataclass
class SyncReport:
    """What a sync sent: how many runs had something new, and how many calls of theirs it replayed."""

    runs: int = 0
    calls: int = 0

    def __str__(self) -> str:
        """One line of it, for the command to print."""
        return f"{self.calls} calls of {self.runs} runs"


def sync(api: BasicDlboardAPI, folder: Path, *, state_path: Path | None = None) -> SyncReport:
    """
    Send what is new in `folder` (runs `OfflineAPI` logged there) to the server, and say how much.

    The state is saved as soon as a run is made on the server, and after each call is replayed (`_resumable`): a sync
    stopped anywhere makes no run twice, and sends again at most the one call it was making. A call the server refuses
    for good is skipped, with a warning; anything else stops the sync, which the next one picks up from.
    """
    state_path = state_path or folder / STATE_FILE
    states = _StateFile.model_validate_json(state_path.read_text()) if state_path.exists() else _StateFile()
    synced = states.servers.setdefault(api.base_url, _ServerSync())
    report = SyncReport()
    for run_folder in sorted(folder.glob(f"{_RUN_PREFIX}*")):
        if not (run_folder / START_FILE).exists():
            continue  # a run still being started: its folder is there, its start not yet
        if (run := synced.runs.get(run_folder.name)) is None:
            run = synced.runs[run_folder.name] = _create(api, synced, run_folder)
            _resumable.save(states, state_path)
        before = report.calls
        for segment in sorted(run_folder.glob("*.jsonl")):
            offset = run.read.get(segment.name, 0)
            for end, line in _lines(segment, offset):
                with _resumable.skipped_if_refused(
                    f"a call of {run_folder.name} ({segment.name}, byte {offset})"
                ):
                    _replay(api, run, run_folder, _RECORD.validate_json(line))
                report.calls += 1
                run.read[segment.name] = offset = end
                _resumable.save(states, state_path)
        report.runs += report.calls > before
    return report


def _create(api: BasicDlboardAPI, synced: _ServerSync, run_folder: Path) -> _RunSync:
    """The run a folder starts, made on the server: in its project and experiment, found (or made) by name."""
    start = _Start.model_validate_json((run_folder / START_FILE).read_text())
    local_experiment = start.run.experiment_id
    if local_experiment not in synced.experiments:
        project_id = api.get_or_create_project(start.project.name, start.project.description).id
        made = (
            api.create_experiment(models.NewExperiment(project_id=project_id, source=start.source))
            if start.experiment is None
            else api.get_or_create_experiment(project_id, start.experiment, source=start.source)
        )
        synced.experiments[local_experiment] = made.id
    experiment_id = synced.experiments[local_experiment]
    run = api.create_run(start.run.model_copy(update={"experiment_id": experiment_id}))
    return _RunSync(run_id=run.id, experiment_id=experiment_id)


def _lines(segment: Path, offset: int) -> Iterator[tuple[int, bytes]]:
    """Each whole line of the segment from byte `offset` on, with the offset just past it: stops at one cut short."""
    with segment.open("rb") as file:
        file.seek(offset)
        for line in file:
            if not line.endswith(b"\n"):
                return
            offset += len(line)
            yield offset, line


def _replay(api: BasicDlboardAPI, run: _RunSync, run_folder: Path, record: _Record) -> None:
    """One call the run made, made of the server, with the run's and its experiment's ids the server gave them."""
    ids: dict[str, Any] = {"run_id": run.run_id, "experiment_id": run.experiment_id}
    match record:
        case _Metrics(body=metrics):
            api.log_metric_batch([logged.model_copy(update=ids) for logged in metrics])
        case _Upload(body=artifact, file=file):
            api.log_artifact_batch([(artifact.model_copy(update=ids), run_folder / _FILES / file)])
        case _Link(body=artifact, ref=ref):
            api.log_artifact_batch([(artifact.model_copy(update=ids), ref)])
        case _Hparams(body=hyperparams):
            api.log_hyperparams(hyperparams.model_copy(update=ids))
        case _Context(body=context):
            api.record_run_context(context.model_copy(update=ids))
        case _Mark(body=mark):
            api.mark_run(run.run_id, pin=mark.pin, keep=mark.keep, tags=mark.tags)
        case _Finish(body=finish):
            api.finish_run(run.run_id, finish.status, ended_at=finish.ended_at)
