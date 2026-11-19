"""
A run being logged to dlboard, from any training loop: the client's own API, with no framework in it.

```python
from dlboard.client import run as dlboard

with dlboard.init("my-project", hparams=config) as run:
    for step, batch in enumerate(loader):
        run.log_metrics({"loss": train_step(batch)}, step=step)
```

`init` creates (or, given a `run_id`, attaches to) a run and starts the two shipping processes that move what it
logs to the server -- metrics, and artifacts -- so the training loop never waits on the network. `DLBoardLogger`
(`dlboard_logger.py`) is the PyTorch Lightning adapter over the same `Run`.
"""

from __future__ import annotations

import atexit
import functools
import json
import signal
import tempfile
import time
import weakref
from argparse import Namespace
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from queue import Full
from typing import TYPE_CHECKING, Any, Final, Generic, Protocol, TypeVar

import numpy as np
import pendulum
import requests
from pydantic import BaseModel, Field, JsonValue, SecretStr
from pydantic_core import to_jsonable_python
from pydantic_settings import BaseSettings

from dlboard import models
from dlboard._batching import BatchParams, Failure, ship_batches
from dlboard._compat import StrEnum
from dlboard._logging import configure_client_logging
from dlboard._mp_context import SPAWN_CONTEXT
from dlboard._telemetry import Attributes, attribute, system
from dlboard._telemetry.system import SystemMetricsSettings
from dlboard._telemetry.traces import CLIENT_SERVICE_NAME, SpanKind, TraceKey, flush, name_service, span
from dlboard.client import _context
from dlboard.client._console import ConsoleCapture, ConsoleChunk, ConsoleSettings
from dlboard.client._context import ContextSettings
from dlboard.client._heartbeat import Heartbeat
from dlboard.client._offline import connect
from dlboard.client._rest_api import DEFAULT_SERVER_URL, BasicDlboardAPI
from dlboard.client._warnings import warn
from dlboard.client.artifacts.figure import Figure
from dlboard.client.histograms import DEFAULT_BINNING
from dlboard.models import Histogram

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from multiprocessing import Queue
    from multiprocessing.context import SpawnProcess
    from multiprocessing.sharedctypes import Synchronized
    from multiprocessing.synchronize import Event as MPEvent
    from types import TracebackType

    from typing_extensions import Self

    from dlboard._wire import RunToken
    from dlboard.client._arrays import ArrayLike
    from dlboard.client._rest_api import RunAPI
    from dlboard.client.artifacts.figure import SavesFigure
    from dlboard.client.histograms import Binning
    from dlboard.models import TagValue
    from dlboard.models._artifact import AnyArtifact

DEFAULT_EXPERIMENT_NAME = "default"

_STARTUP_WARN_THRESHOLD_SEC = 2.0
"""How long a worker process's import + startup can take before we warn it's the likely cause of
any backpressure warnings seen right after training starts, rather than dlboard itself."""
_STARTUP_WAIT_TIMEOUT_SEC = 60.0
"""Cap on how long starting a run waits for a worker to report ready, so a wedged import can't hang forever."""
_FLUSH_TIMEOUT_SEC = 60.0
"""Cap on how long `flush` waits for a worker to ship everything queued, so a dead server can't hang it."""
_SHIP_INTERVAL_SEC = 5.0
"""How long a worker lets a partial batch sit before shipping it anyway."""
_KEYS_PER_QUEUED_METRICS: Final = 1000
"""
How many keys one queued `LoggedMetrics` carries. A wider call is queued as several, so a batch of
`metrics_q_flush_size` items stays a bounded upload however many keys each step logged.
"""
_TRANSIENT_CLIENT_ERRORS: Final = frozenset({HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS})
_ITEM_CLIENT_ERRORS: Final = frozenset(
    {
        HTTPStatus.BAD_REQUEST,
        HTTPStatus.CONFLICT,
        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
        HTTPStatus.UNPROCESSABLE_ENTITY,
    }
)
"""The 4xx a server answers about what a batch holds, so a batch without the offending item can still land."""


def _split_keys(metrics: Mapping[str, float]) -> list[dict[str, float]]:
    """`metrics` in chunks of at most `_KEYS_PER_QUEUED_METRICS` keys, in order; one chunk if it is empty."""
    items = list(metrics.items())
    chunks = [
        dict(items[start : start + _KEYS_PER_QUEUED_METRICS])
        for start in range(0, len(items), _KEYS_PER_QUEUED_METRICS)
    ]
    return chunks or [dict(metrics)]


def warn_if_startup_was_slow(elapsed_sec: float) -> None:
    """Warn that any backpressure warnings so far are from worker startup, not sustained throughput."""
    if elapsed_sec > _STARTUP_WARN_THRESHOLD_SEC:
        warn(
            f"dlboard's logging worker processes took {elapsed_sec:.1f}s to start up. This is "
            "almost always import overhead from your training script or its other dependencies, not "
            "dlboard -- any backpressure warnings logged in the next few steps are a side effect of "
            "this startup delay rather than an ongoing throughput problem."
        )


def classify_failure(exc: Exception) -> Failure:
    """
    What a failure to ship a batch says about it (see `Failure`).

    Transient: the server or network is down or busy -- a connection error, a timeout, a 5xx, or a 408/429, which
    only ask to retry later. Per item: something in the batch can never be sent, so the batch is split to find it --
    a 4xx about what was sent (an invalid item, a duplicate, a batch grown past `DLBOARD_MAX_UPLOAD_MB` while the
    server was down), or an error here, such as a `File` whose path is gone. Whole: a 4xx that refuses the request
    itself, whatever is in it -- credentials, access, a run that is gone, a locked project -- so no part of it lands.
    """
    match exc:
        case requests.ConnectionError() | requests.Timeout():
            return Failure.TRANSIENT
        case requests.HTTPError(response=requests.Response() as response):
            status = response.status_code
            if status >= HTTPStatus.INTERNAL_SERVER_ERROR or status in _TRANSIENT_CLIENT_ERRORS:
                return Failure.TRANSIENT
            return Failure.PER_ITEM if status in _ITEM_CLIENT_ERRORS else Failure.WHOLE
        case _:
            return Failure.PER_ITEM


def _ship_artifacts(api: RunAPI, experiment_id: int, run_id: int, batch: list[Sequence[AnyArtifact]]) -> None:
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


_RESTART_INTERVAL_SEC = 10.0
"""The soonest a shipping process that died is started again: one that dies on starting must not spin."""


@dataclass(frozen=True)
class _Worker(Generic[T]):
    """One shipping process, and what the training process talks to it through."""

    queue: Queue[T | None]
    process: SpawnProcess
    ready: MPEvent
    flushed: MPEvent
    refused: Synchronized[int]
    """How many items the server has refused for good (see `classify_failure`), counted by the process itself."""

    def stop(self) -> None:
        """End the process, and let go of the queue without waiting on what it still held."""
        self.process.terminate()
        self.process.join(timeout=1)
        self.queue.cancel_join_thread()
        self.queue.close()


@dataclass
class _Shipper(Generic[T]):
    """A shipping process kept running (started again whenever it is found dead), plus the queue feeding it."""

    name: str
    start: Callable[[], _Worker[T]]
    worker: _Worker[T] = field(init=False)
    started_at: float = field(init=False)
    unflushed: bool = field(default=False, init=False)
    """Whether anything was queued since the last flush that saw it shipped."""
    _dropped_total: int = field(default=0, init=False)
    _dropped_since_warning: int = field(default=0, init=False)
    _last_drop_warning: float = field(default=0.0, init=False)
    _refused_reported: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.worker, self.started_at = self.start(), time.monotonic()

    def put(self, item: T) -> None:
        """
        Queue `item` for shipping, never blocking and never raising: training must never stall on dlboard.

        A shipping process found dead is started again, on a queue of its own (a process killed while reading
        dies holding the old one's lock), at most once every `_RESTART_INTERVAL_SEC`; until then, and whenever the
        queue is full (the process can't keep up, or the server/network is down), `item` is dropped and counted,
        warning at most once every `_DROP_WARNING_INTERVAL_SEC` while it keeps happening, plus a final total once
        the run is flushed.
        """
        if not self.worker.process.is_alive() and not self._restart():
            self._drop("its shipping process died, and is started again shortly")
            return
        try:
            self.worker.queue.put_nowait(item)
            self.unflushed = True
        except Full:
            self._drop(
                "its queue is full -- the shipping process can't keep up, or the server/network is down; see "
                "`RunSettings` to size the queue, or check the server"
            )

    def _restart(self) -> bool:
        """Start the dead process again, unless it was started too recently; whether it was."""
        if time.monotonic() - self.started_at < _RESTART_INTERVAL_SEC:
            return False
        dead = self.worker
        try:
            lost = f"the {dead.queue.qsize()} item(s) it still held are lost"
        except NotImplementedError:  # macOS cannot count a queue's items
            lost = "whatever it still held is lost"
        self._report_refused()
        dead.stop()
        self.worker, self.started_at = self.start(), time.monotonic()
        warn(
            f"dlboard's {self.name} shipping process died (exit code {dead.process.exitcode}) and was started again; "
            f"{lost}."
        )
        return True

    def _drop(self, why: str) -> None:
        self._dropped_total += 1
        self._dropped_since_warning += 1
        now = time.monotonic()
        if now - self._last_drop_warning > _DROP_WARNING_INTERVAL_SEC:
            warn(
                f"dlboard dropped {self._dropped_since_warning} {self.name} item(s) rather than block training: {why}."
            )
            self._dropped_since_warning = 0
            self._last_drop_warning = now

    def _report_refused(self) -> None:
        """Warn, here in the training process, about what the server refused since the last report."""
        refused = self.worker.refused.value - self._refused_reported
        if refused:
            warn(
                f"The dlboard server refused {refused} {self.name} item(s), which are dropped: the shipping "
                "process's log says why."
            )
        self._refused_reported = self.worker.refused.value

    def flush(self, deadline: float) -> bool:
        """
        Wait, until `deadline` (`time.monotonic`) at the latest, for everything queued to be shipped; whether it was.

        Something the server refused for good counts as gone, and is warned about here; so is what was dropped.
        """
        if self._dropped_total:
            warn(
                f"dlboard dropped {self._dropped_total} {self.name} item(s) during this run -- see the warnings above for when."
            )
            self._dropped_total = 0
        if not self.unflushed:
            return True
        if not self.worker.process.is_alive():
            warn(f"dlboard's {self.name} shipping process is dead, so what it held cannot be shipped")
            return False
        self.worker.flushed.clear()
        try:
            self.worker.queue.put(None, timeout=max(0.0, deadline - time.monotonic()))
            shipped = self.worker.flushed.wait(max(0.0, deadline - time.monotonic()))
        except Full:
            shipped = False
        self._report_refused()
        if not shipped:
            warn(
                f"dlboard's {self.name} still hadn't all been shipped after {_FLUSH_TIMEOUT_SEC}s -- "
                "anything not yet shipped is lost if this process exits now."
            )
            return False
        self.unflushed = False
        return True

    def close(self) -> None:
        """Ship what is still queued (as `flush` would), then stop the process: this run is done with it."""
        self.flush(time.monotonic() + _FLUSH_TIMEOUT_SEC)
        self.worker.stop()


def _ship_until_parent_says_stop(
    q: Queue[T | None],
    ship: Callable[[list[T]], None],
    params: BatchParams,
    refused: Synchronized[int],
) -> None:
    """
    `ship_batches` in a shipping process, which ignores Ctrl+C so the training process owns shutdown.

    A terminal's Ctrl+C goes to the whole foreground process group, shipping processes included. Without
    this, they'd die mid-batch with a KeyboardInterrupt, and the training process's final flush would have
    nothing left to ship to. Ignoring SIGINT here lets the parent flush what's queued, then exit, which also
    takes these daemons down. What the server refuses for good is counted in `refused`, for the training process to
    say so where it is seen.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    configure_client_logging()
    name_service(CLIENT_SERVICE_NAME)

    def count_refused(count: int) -> None:
        with refused.get_lock():
            refused.value += count

    # Daemons are killed at exit with whatever spans they had not exported yet: a flush is where they go out.
    ship_batches(q, ship, params, classify=classify_failure, on_flush=flush, on_drop=count_refused)


class _ShipKind(StrEnum):
    METRICS = "metrics"
    ARTIFACTS = "artifacts"


@dataclass(frozen=True, slots=True)
class _ShipTrace(Attributes):
    kind: _ShipKind = attribute(TraceKey.SHIP_KIND)
    items: int = attribute(TraceKey.ITEMS)
    run_id: int = attribute(TraceKey.RUN_ID)
    experiment_id: int = attribute(TraceKey.EXPERIMENT_ID)


def _ship_in_span(
    kind: _ShipKind, experiment_id: int, run_id: int, ship: Callable[[list[T]], None], batch: list[T]
) -> None:
    """Ship `batch` as one trace of its own: the requests it takes are children, and the server joins them."""
    with span(
        "dlboard.ship",
        SpanKind.INTERNAL,
        _ShipTrace(kind=kind, items=len(batch), run_id=run_id, experiment_id=experiment_id),
    ):
        ship(batch)


def _start_worker(  # noqa: PLR0913 -- one shipper, fully described
    kind: _ShipKind,
    ship: Callable[[list[T]], None],
    *,
    experiment_id: int,
    run_id: int,
    q_size: int,
    flush_size: int,
) -> _Worker[T]:
    queue: Queue[T | None] = SPAWN_CONTEXT.Queue(maxsize=q_size)
    ready, flushed = SPAWN_CONTEXT.Event(), SPAWN_CONTEXT.Event()
    refused: Synchronized[int] = SPAWN_CONTEXT.Value("q", 0)
    process = SPAWN_CONTEXT.Process(
        target=_ship_until_parent_says_stop,
        args=(
            queue,
            functools.partial(_ship_in_span, kind, experiment_id, run_id, ship),
            BatchParams(flush_size, _SHIP_INTERVAL_SEC, ready, flushed),
            refused,
        ),
        name=f"dlboard-{kind}",
        daemon=True,
    )
    process.start()
    return _Worker(queue, process, ready, flushed, refused)


class RunSettings(BaseModel, frozen=True, extra="forbid"):
    """Tunables for a run: what it records about what produced it, and its background shipping queues."""

    context: ContextSettings = Field(default_factory=ContextSettings)
    """What a new run records about its code, command and environment (`DLBOARD_CONTEXT_*` by default)."""
    system_metrics: SystemMetricsSettings = Field(default_factory=SystemMetricsSettings)
    """Whether, and how often, the run samples CPU, memory and GPUs (`DLBOARD_SYSTEM_METRICS_*` by default)."""
    console: ConsoleSettings = Field(default_factory=ConsoleSettings)
    """Whether, and how, the run captures what the script prints (`DLBOARD_CONSOLE_*` by default)."""

    metrics_q_size: int = 500
    """Max metric batches buffered before `log_metrics` starts warning about backpressure."""
    metrics_q_flush_size: int = 100
    """Metric batches accumulated before the background process ships them to the server."""
    artifact_q_size: int = 100
    """Max artifact batches buffered before `log_artifact` starts warning about backpressure."""
    artifact_q_flush_size: int = 10
    """Artifact batches accumulated before the background process ships them to the server."""


class LaunchEnv(BaseSettings):
    """What the environment a run is launched in says: a run to attach to, and this process's rank."""

    dlboard_run_id: int | None = None
    """An existing run to attach to instead of creating one -- see `init`'s `run_id`."""
    rank: int | None = None
    local_rank: int | None = None
    slurm_procid: int | None = None
    jsm_namespace_rank: int | None = None

    @property
    def global_rank(self) -> int:
        """
        The global rank a launcher (torchrun, accelerate, SLURM, jsrun) gave this process: 0 when none did.

        The same variables, in the same order, that Lightning's `rank_zero_only` reads, so a script logging with
        both picks the same rank as the one that logs.
        """
        ranks = (self.rank, self.local_rank, self.slurm_procid, self.jsm_namespace_rank)
        return next((rank for rank in ranks if rank is not None), 0)


def _jsonable_fallback(value: object) -> JsonValue:  # dlboard-ignore[forbid-string-ignore]
    """What pydantic's JSON serializer can't convert itself: numpy scalars, `Namespace`s, anything else as text."""
    match value:
        case np.generic():
            return value.item()
        case Namespace():
            return vars(value)
        case _:
            return str(value)


def _flatten_json(value: JsonValue, prefix: str) -> dict[str, models.ValidJsonTypes]:
    """Join nested dict keys with `"/"`, and keep a list as one JSON string -- a hparam is a single cell."""
    match value:
        case dict():
            return {
                key: leaf
                for name, child in value.items()
                for key, leaf in _flatten_json(child, f"{prefix}/{name}" if prefix else name).items()
            }
        case list():
            return {prefix: json.dumps(value)}
        case str() | int() | float() | bool() | None:
            return {prefix: value}


def flatten_hparams(params: Mapping[str, Any] | Namespace | BaseModel) -> models.FlatHparamDict:
    """
    Turn `params` into the flat `{"a/b": scalar}` dict a hyperparameter blob stores, like Lightning's loggers do.

    Pydantic does the serializing, so a `BaseModel`, a pydantic dataclass (and its own aliases and
    field serializers), a stdlib dataclass, a `Namespace`, an enum or a tuple all just work, nested
    arbitrarily. Nested keys are joined with `"/"`, which is also how the experiment page groups
    them. A list stays one value (as a JSON string). Scalars pass through untouched.
    """
    return _flatten_json(to_jsonable_python(params, fallback=_jsonable_fallback), "")


def _place_run(  # noqa: PLR0913 -- where a run can be given to go, each once
    api: RunAPI,
    *,
    project_id: int | None,
    experiment_id: int | None,
    run_id: int | None,
    source: models.ExperimentSource,
    name: str | None,
) -> tuple[int, int, bool]:
    """
    The experiment and run a run logs to, and whether the run was created for it: a run token's, `run_id`, or a new one.

    Asks who the credentials are first, before any worker starts: the workers never retry a 4xx (see `classify_failure`),
    so bad credentials would otherwise only show up as silently lost metrics.
    """
    token_run = api.whoami().run_id
    if token_run is not None:
        if run_id not in (None, token_run):
            msg = f"DLBOARD_API_KEY is a run token for run {token_run}, not run {run_id}"
            raise ValueError(msg)
        run_id = token_run
    if run_id is not None:
        run = api.get_run(run_id)
        if experiment_id not in (None, run.experiment_id):
            msg = f"run {run_id} belongs to experiment {run.experiment_id}, not {experiment_id}"
            raise ValueError(msg)
        return run.experiment_id, run_id, False
    if experiment_id is None:
        if project_id is None:
            msg = "a new run needs a project or an experiment to go in"
            raise ValueError(msg)
        experiment_id = api.create_experiment(models.NewExperiment(project_id=project_id, source=source)).id
    # `name` only when given: `NewRun` names a run itself when the field is omitted, never when it is `None`.
    new_run = models.NewRun(experiment_id=experiment_id, **({} if name is None else {"name": name}))
    return experiment_id, api.create_run(new_run).id, True


def call_if_alive(method: weakref.WeakMethod[Callable[[], None]]) -> None:
    """Call `method`, unless its object is already gone."""
    if (bound := method()) is not None:
        bound()


def _close_shippers(*shippers: _Shipper[Any]) -> None:
    """Ship what a run nothing uses any more still had queued, and stop its shipping processes."""
    for shipper in shippers:
        shipper.close()


class _Stops(Protocol):
    """What a run keeps running beside it until it finishes: its system metrics sampler, its console capture."""

    def stop(self) -> None:
        """Stop, logging into the run what is still held."""
        ...


def _log_system(run: Callable[[], Run | None], values: Mapping[str, float]) -> None:
    """
    Log a system metrics sample into `run` (a weak reference: the sampler must not keep it alive), at the time taken.

    Its step is the Unix time it was taken, not a training step (see `models.SYSTEM_METRIC_PREFIX`).
    """
    if (alive := run()) is not None:
        alive.log_metrics(values, step=int(time.time()))


def _log_console(run: Callable[[], Run | None], chunk: ConsoleChunk) -> None:
    """Log a chunk of console output into `run` (a weak reference, as `_log_system`'s), as the artifact it is."""
    if (alive := run()) is not None:
        alive.log_artifact([chunk])


@dataclass(frozen=True)
class _Shipping:
    """A started run's server, ids and shipping processes -- what only the process that logs has."""

    api: RunAPI
    experiment_id: int
    run_id: int
    metrics: _Shipper[models.LoggedMetrics]
    artifacts: _Shipper[Sequence[AnyArtifact]]


class Run:
    """
    A run being logged to: what is logged is validated here, at the call, then queued for the shipping processes.

    Nothing here blocks on the server but `flush`/`finish` (each bounded by `_FLUSH_TIMEOUT_SEC`), and nothing
    raises because the server is down or slow: what can't be queued is dropped, counted and warned about. As a
    context manager it finishes the run as `FINISHED`, or `FAILED` if the block raised. A run never finished is
    flushed at interpreter exit, as `UNKNOWN` -- a script that ended and one that crashed look the same from here.

    A run on a non-zero rank (see `LaunchEnv.global_rank`) is inert: it makes no server calls, starts no
    processes, and drops whatever it is asked to log, so one run is created per job, not per GPU.
    """

    def __init__(self, shipping: _Shipping | None, *, run_id: int | None = None) -> None:
        """Use `init` (or `Run.start`); an inert run is `Run(None)`, with the `run_id` it would log to if known."""
        self._shipping = shipping
        self._inert_run_id = run_id
        self._helpers: list[_Stops] = []
        self.status: models.RunStatus | None = None
        """How this process last told the server the run ended, if it has."""
        if shipping is None:
            return
        # The shipping processes are daemons -- killed with whatever's still queued the moment the interpreter
        # exits, unless something flushes them first. Held weakly, so a run nothing uses any more (one per trial of
        # a sweep) is collected, and its processes stopped with it.
        self._exit_hook = functools.partial(call_if_alive, weakref.WeakMethod(self._finish_at_exit))
        atexit.register(self._exit_hook)
        weakref.finalize(self, _close_shippers, shipping.metrics, shipping.artifacts)

    @classmethod
    def start(  # noqa: PLR0913 -- one keyword per thing a run is started with
        cls,
        api: RunAPI,
        *,
        project_id: int | None,
        experiment_id: int | None,
        run_id: int | None,
        source: models.ExperimentSource,
        settings: RunSettings | None = None,
        name: str | None = None,
        pin: models.PinColor | None = None,
        keep: bool = False,
        tags: Sequence[str] = (),
    ) -> Self:
        """
        Attach to `run_id`, or create a run (in `experiment_id`, or a new experiment of `project_id`); start shipping.

        A `run_id` is checked to belong to `experiment_id`, if both are given. A run token's run is the run, whatever
        else is given (and another `run_id` an error). `pin` and `keep` mark the run the moment it exists; a server that
        refuses fails this call, before anything starts logging.
        """
        settings = settings or RunSettings()
        configure_client_logging()
        name_service(CLIENT_SERVICE_NAME)
        experiment_id, run_id, created = _place_run(
            api, project_id=project_id, experiment_id=experiment_id, run_id=run_id, source=source, name=name
        )
        if pin is not None or keep or tags:
            api.mark_run(run_id, pin=pin, keep=keep, tags=tags)
        startup_start = time.perf_counter()
        metrics: _Shipper[models.LoggedMetrics] = _Shipper(
            _ShipKind.METRICS,
            functools.partial(
                _start_worker,
                _ShipKind.METRICS,
                api.log_metric_batch,
                experiment_id=experiment_id,
                run_id=run_id,
                q_size=settings.metrics_q_size,
                flush_size=settings.metrics_q_flush_size,
            ),
        )
        ship_artifacts: Callable[[list[Sequence[AnyArtifact]]], None] = functools.partial(
            _ship_artifacts, api, experiment_id, run_id
        )
        artifacts: _Shipper[Sequence[AnyArtifact]] = _Shipper(
            _ShipKind.ARTIFACTS,
            functools.partial(
                _start_worker,
                _ShipKind.ARTIFACTS,
                ship_artifacts,
                experiment_id=experiment_id,
                run_id=run_id,
                q_size=settings.artifact_q_size,
                flush_size=settings.artifact_q_flush_size,
            ),
        )
        for shipper in (metrics, artifacts):
            if not shipper.worker.ready.wait(_STARTUP_WAIT_TIMEOUT_SEC):
                warn(
                    f"dlboard's {shipper.name} shipping process hadn't started after {_STARTUP_WAIT_TIMEOUT_SEC}s; "
                    "what is logged queues up until it does."
                )
        warn_if_startup_was_slow(time.perf_counter() - startup_start)
        started = cls(_Shipping(api, experiment_id, run_id, metrics, artifacts))
        if created:  # a run attached to keeps the context it was started with
            started._record_context(settings.context)
        alive = weakref.ref(started)  # held weakly, so a run nothing uses any more is still collected
        started._attach(system.start(settings.system_metrics, functools.partial(_log_system, alive)))
        started._attach(ConsoleCapture.start(settings.console, functools.partial(_log_console, alive)))
        started._attach(Heartbeat.start(functools.partial(api.heartbeat, run_id)))
        return started

    def _attach(self, helper: _Stops | None) -> None:
        """Keep `helper` (a sampler, a console capture) running until the run finishes, or is collected."""
        if helper is not None:
            self._helpers.append(helper)
            weakref.finalize(self, helper.stop)

    def _record_context(self, settings: ContextSettings) -> None:
        """Record what produced this run, and queue its uncommitted changes. Never fails the script."""
        if (shipping := self._shipping) is None:
            return
        context, diff = _context.collect(shipping.run_id, shipping.experiment_id, settings)
        try:
            shipping.api.record_run_context(context)
        except requests.RequestException as exc:  # an older server has no such route: a 404
            warn(f"dlboard could not record what produced this run (its commit, command and packages): {exc}")
        if diff is not None:
            self.log_artifact([diff])

    @property
    def run_id(self) -> int | None:
        """The run logged to -- hand it to other processes' `run_id=` (or `DLBOARD_RUN_ID`) to share the run."""
        return self._inert_run_id if self._shipping is None else self._shipping.run_id

    @property
    def experiment_id(self) -> int | None:
        """The experiment the run is in; `None` on a non-zero rank, which never asks the server."""
        return None if self._shipping is None else self._shipping.experiment_id

    @property
    def log_context(self) -> dict[str, int]:
        """
        The run's ids, to bind into the script's own logs: `structlog.contextvars.bind_contextvars(**run.log_context)`.

        Logs already sent to Loki (or anywhere else) can then be found by the run, without dlboard becoming a log store.
        Empty on a non-zero rank, which has no run of its own.
        """
        shipping = self._shipping
        if shipping is None:
            return {}
        return {"dlboard.run_id": shipping.run_id, "dlboard.experiment_id": shipping.experiment_id}

    def log_hyperparams(self, params: Mapping[str, Any] | Namespace | BaseModel) -> None:
        """
        Log hyperparameters.

        Nested configs (pydantic models and dataclasses, stdlib dataclasses, dicts, ...) are flattened
        to `"parent/child"` keys -- see `flatten_hparams`. Scalar values are stored and later displayed
        exactly as given -- dlboard never guesses at or rewrites a value's type. A `Namespace` built from
        `argparse` without a `type=` on `add_argument` hands every value over as a string (e.g. `"128"`, not
        `128`); that's the usual cause of a hyperparameter column that looks numeric but sorts/compares as text.

        A run keeps the first hyperparameters logged to it. Logging others later (a second stage, or another process
        sharing the run) changes nothing, and warns.
        """
        if (shipping := self._shipping) is None:
            return
        sent = models.NewHyperParams.from_raw(
            run_id=shipping.run_id, experiment_id=shipping.experiment_id, hparams=flatten_hparams(params)
        )
        if shipping.api.log_hyperparams(sent).hparams_dict != sent.hparams_dict:
            warn(
                f"dlboard run {shipping.run_id} already has hyperparameters, which it keeps: these were not "
                "recorded. Log every hyperparameter in one call, or give each process its own run."
            )

    def log_metrics(self, metrics: Mapping[str, float], step: int) -> None:
        """
        Validate and timestamp `metrics` right here, at the call site, then queue them for shipping.

        Validating here (not in the shipping process) raises a bad metric at the line that logged
        it, instead of the server rejecting a whole batch of otherwise-good metrics later on. `step` is required:
        a counter kept here could not agree with another process sharing the run, or with a resumed one.
        """
        if (shipping := self._shipping) is None:
            return
        timestamp_utc = pendulum.now(pendulum.UTC)
        for chunk in _split_keys(metrics):
            shipping.metrics.put(
                models.LoggedMetrics(
                    metrics=chunk,
                    step=step,
                    experiment_id=shipping.experiment_id,
                    run_id=shipping.run_id,
                    timestamp_utc=timestamp_utc,
                )
            )

    def log_histograms(
        self,
        histograms: Mapping[str, ArrayLike | Histogram],
        step: int,
        *,
        binning: Binning = DEFAULT_BINNING,
    ) -> None:
        """
        Bin each of `histograms`' values right here, at the call site, then queue the buckets for shipping.

        Only edges and counts ever leave this process, so the cost of a big tensor or a fine binning is paid
        by the script that chose it -- once, in one pass over the values -- and a binning that cannot fit
        (too many buckets) raises at this line. A `Histogram` you already binned is sent as it is.
        """
        if (shipping := self._shipping) is None:
            return
        shipping.metrics.put(
            models.LoggedMetrics(
                histograms={
                    key: values if isinstance(values, Histogram) else binning.histogram(values)
                    for key, values in histograms.items()
                },
                step=step,
                experiment_id=shipping.experiment_id,
                run_id=shipping.run_id,
                timestamp_utc=pendulum.now(pendulum.UTC),
            )
        )

    def log_artifact(self, artifacts: Sequence[AnyArtifact]) -> None:
        """
        Queue `artifacts` for encoding and upload.

        Warns about what is wrong within this call: an overlay (`Overlay.over`) of an image it does not also log, and
        a key logged twice at one step (a chart shows one per step, so two overlays of one image need a `name` each).
        """
        if (shipping := self._shipping) is None:
            return
        keys = {artifact.key for artifact in artifacts}
        slots: set[tuple[str, int]] = set()
        for artifact in artifacts:
            if (parsed := models.OverlayKey.parse(artifact.key)) and (image := parsed.image_key) not in keys:
                warn(
                    f"{artifact.key!r} is an overlay of {image!r}, but nothing is logged under that key in this call, "
                    "so no image chart will draw it unless that image is logged some other way."
                )
            if (slot := (artifact.key, artifact.step)) in slots:
                warn(
                    f"{artifact.key!r} is logged twice at step {artifact.step}, and a chart shows only one of them. "
                    "Log it under another key (for an overlay, give it a `name`)."
                )
            slots.add(slot)
        shipping.artifacts.put(artifacts)

    def log_figure(
        self,
        figure: SavesFigure,
        key: str,
        step: int,
        *,
        tags: Mapping[str, TagValue] | None = None,
        save_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """
        Render `figure` to an image now and queue it as an artifact, like `mlflow.log_figure`.

        Rendered here, at the call site, so the figure can be closed or mutated straight after.
        The format is PNG unless `key` ends in another supported extension (`"loss.svg"`) or
        `save_kwargs` has a `format`; `key` itself is used as given. `save_kwargs` goes to
        `figure.savefig`.
        """
        if self._shipping is None:
            return
        self.log_artifact([Figure.from_figure(figure, key, step, tags=tags, save_kwargs=save_kwargs)])

    @property
    def unflushed(self) -> bool:
        """Whether anything was logged since the last flush that saw it shipped."""
        shipping = self._shipping
        return shipping is not None and (shipping.metrics.unflushed or shipping.artifacts.unflushed)

    def flush(self) -> bool:
        """
        Block until everything logged so far has reached the server; whether it all did.

        Waits `_FLUSH_TIMEOUT_SEC` at the most, for both shipping processes together, however long the server has
        been down, and warns about anything that didn't make it.
        """
        if (shipping := self._shipping) is None:
            return True
        deadline = time.monotonic() + _FLUSH_TIMEOUT_SEC
        metrics_shipped = shipping.metrics.flush(deadline)
        return shipping.artifacts.flush(deadline) and metrics_shipped

    def finish(self, status: models.RunStatus = models.RunStatus.FINISHED) -> None:
        """
        Stop sampling system metrics and capturing the console, flush, then tell the server the run ended, and how.

        Never fails the script. A run can go on logging after this (a test stage after fit), and finishing it again
        moves its end later.
        """
        if (shipping := self._shipping) is None:
            return
        for helper in self._helpers:  # a console capture cuts its last chunk as it stops, so before the flush
            helper.stop()
        self._helpers.clear()
        self.flush()
        try:
            shipping.api.finish_run(shipping.run_id, status)
            self.status = status
        except requests.RequestException as exc:
            warn(f"dlboard could not record that the run {status.value}: {exc}")

    def __enter__(self) -> Self:
        """The run itself, finished when the block ends."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Finish the run: `FAILED` if the block raised (Ctrl+C included), `FINISHED` otherwise."""
        del exc, traceback
        self.finish(models.RunStatus.FINISHED if exc_type is None else models.RunStatus.FAILED)

    def _finish_at_exit(self) -> None:
        """Catch a crash, or a script that never finished its run."""
        if self.unflushed or self.status is None:
            warn(
                "dlboard's run was never finished -- flushing whatever's still queued now, at interpreter exit. Call "
                "`run.finish()` yourself (or use the run as a context manager) to avoid this delaying process exit."
            )
            if self.status is None:
                self.finish(models.RunStatus.UNKNOWN)
            else:
                self.flush()


def resolve_names(
    api: RunAPI, project: str, experiment: str, *, description: str, source: models.ExperimentSource
) -> tuple[int | None, int | None]:
    """
    The ids of `project` and its `experiment`, creating either that doesn't exist yet, once the credentials are checked.

    Neither, for a run token (`run_token`): it comes with its run, which is where the run goes, and may not look anything
    else up.
    """
    if api.whoami().run_id is not None:
        return None, None
    project_id = api.get_or_create_project(project, description=description).id
    return project_id, api.get_or_create_experiment(project_id, name=experiment, source=source).id


def run_token(  # noqa: PLR0913 -- where the run goes, each once, and the server and key to ask with
    project: str | None = None,
    experiment: str = DEFAULT_EXPERIMENT_NAME,
    *,
    name: str | None = None,
    run_id: int | None = None,
    server_url: str = DEFAULT_SERVER_URL,
    api_key: SecretStr | None = None,
) -> RunToken:
    """
    Start a run in `experiment` of `project` (or take `run_id`'s) and mint a token that logs to it alone, for a job.

    Run where the job is submitted, with your own key (`api_key`, default: the `DLBOARD_API_KEY` env var); hand the job
    the token's `token` as its `DLBOARD_API_KEY`, and its `init` or `DLBoardLogger` logs to this run, whatever project
    the job's script names. The token can do nothing but that, and stops working at its `expires_at` (the server's
    `DLBOARD_RUN_TOKEN_LIFETIME_HOURS`). `dlboard run-token` is this, from a shell. Always asks a server: a run in an
    offline folder has no token.
    """
    api = BasicDlboardAPI(server_url, api_key)
    if run_id is None:
        if project is None:
            msg = "give a project (and experiment) to start a run in, or an existing run's id"
            raise ValueError(msg)
        _, experiment_id = resolve_names(
            api, project, experiment, description="", source=models.ExperimentSource.PYTHON
        )
        if experiment_id is None:
            msg = "DLBOARD_API_KEY is a run token already: mint one with your own key"
            raise ValueError(msg)
        # `name` only when given: `NewRun` names a run itself when the field is omitted, never when it is `None`.
        new_run = models.NewRun(experiment_id=experiment_id, **({} if name is None else {"name": name}))
        run_id = api.create_run(new_run).id
    return api.run_token(run_id)


def init(  # noqa: PLR0913 -- one keyword per thing a run is started with
    project: str,
    experiment: str = DEFAULT_EXPERIMENT_NAME,
    *,
    hparams: Mapping[str, Any] | Namespace | BaseModel | None = None,
    name: str | None = None,
    project_description: str = "",
    server_url: str = DEFAULT_SERVER_URL,
    settings: RunSettings | None = None,
    run_id: int | None = None,
    pin: models.PinColor | None = None,
    keep: bool = False,
    tags: Sequence[str] = (),
) -> Run:
    """
    Start a run in `experiment` of `project` (creating either by name if it doesn't exist yet), from any script.

    `run_id` (default: the `DLBOARD_RUN_ID` env var) attaches to an existing run of that experiment instead -- for
    resuming a run, or sharing one between processes launched separately. With a run token as `DLBOARD_API_KEY`
    (`dlboard run-token`), the run is the token's, and `project` and `experiment` aren't looked up. `name` names a new run (one is made up
    otherwise), `hparams` are logged straight away (see `Run.log_hyperparams`), and `pin`/`keep` mark the run the
    moment it exists, as do `tags` (`["baseline"]`, shared by the project). `project_description` only applies the first
    time `project` is seen.

    On a non-zero rank this makes no server calls and returns an inert run (see `Run`): call it on every rank.
    """
    env = LaunchEnv()
    run_id = run_id if run_id is not None else env.dlboard_run_id
    if env.global_rank != 0:
        # Every rank runs this, and a get-or-create isn't safe to race: rank 0 alone resolves the names.
        return Run(None, run_id=run_id)
    api = connect(server_url)
    project_id, experiment_id = resolve_names(
        api, project, experiment, description=project_description, source=models.ExperimentSource.PYTHON
    )
    run = Run.start(
        api,
        project_id=project_id,
        experiment_id=experiment_id,
        run_id=run_id,
        source=models.ExperimentSource.PYTHON,
        settings=settings,
        name=name,
        pin=pin,
        keep=keep,
        tags=tags,
    )
    if hparams is not None:
        run.log_hyperparams(hparams)
    return run
