"""
Operational metrics and logs for a Prometheus scrape or an OpenTelemetry (OTLP) collector -- off unless asked for.

Everything an instrumented module touches lives here and imports nothing from `opentelemetry`: an
instrument (`Counter`, `UpDownCounter`, `Histogram`) is a plain value that's free to define at module
level, and `.add()`/`.record()` on it is one pid comparison while telemetry is off. The OpenTelemetry
SDK is only imported (from `_sdk.py`) by the first call that finds telemetry switched on, so a
local deployment never loads it, never logs about it, and works without the `otel` extra.

The SDK objects are built lazily, once per *process*, by `_PerProcess`:

- Nothing is built at import time, so a collector that's down or a typo'd endpoint can't break
  startup; a build that fails logs one warning and leaves that process uninstrumented.
- `fork` (Granian's workers on Python 3.12) and `spawn` (the blob writer, `dlboard._mp_context`; Granian
  on 3.14+) both end up in the same place: each process builds its own providers on first use, with
  its own `service.instance.id`. A provider is deliberately never built in the process that goes on
  to fork -- the OTel SDK's exporters restart their threads in a forked child, which would export
  the parent's cumulative counts a second time under the parent's identity.

Settings are `DLBOARD_TELEMETRY_*`; the exporters themselves read the standard `OTEL_*` variables.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from enum import StrEnum
from multiprocessing import util
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Protocol, Self, override

from pydantic import PositiveFloat, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from structlog.stdlib import get_logger

from dlboard.serve._settings_env import set_setting_env

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Iterator

    from dash import Dash
    from opentelemetry.metrics import Meter
    from opentelemetry.util.types import Attributes as OtelAttributes
    from opentelemetry.util.types import AttributeValue

    from dlboard.serve.telemetry._sdk import Logs, Metrics

_log = get_logger(__name__)


class MetricsExporter(StrEnum):
    """Where metrics go."""

    OTLP = "otlp"
    """Pushed over OTLP/HTTP to a collector, Mimir, Grafana Cloud, ..."""
    PROMETHEUS = "prometheus"
    """Scraped from `/metrics`."""


class TelemetrySettings(BaseSettings):
    """`DLBOARD_TELEMETRY_*` environment variables. With none of them set, no telemetry code runs at all."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_TELEMETRY_")

    metrics: Annotated[frozenset[MetricsExporter], NoDecode] = frozenset()
    """Comma-separated exporters (`otlp`, `prometheus`) to send metrics to. Unset exports none."""
    logs: bool = False
    """Also send every log event over OTLP."""
    prometheus_dir: Path | None = None
    """
    A directory for the Prometheus exporter's per-process snapshots, so `/metrics` can add up every worker.

    `dlboard serve` makes a fresh one inside it (or in the system temp dir when unset) for each run.
    """
    prometheus_snapshot_s: PositiveFloat = 15
    """How often each process writes its snapshot -- how stale another worker's numbers on `/metrics` can be."""

    @field_validator("metrics", mode="before")
    @classmethod
    def _split_commas(cls, value: Any) -> Any:  # noqa: ANN401 -- a `mode="before"` validator sees the raw input
        return [v.strip() for v in value.split(",") if v.strip()] if isinstance(value, str) else value

    @model_validator(mode="after")
    def _require_extra(self) -> Self:
        if (self.metrics or self.logs) and not _sdk_installed():
            msg = "DLBOARD_TELEMETRY_* is set, but the OpenTelemetry SDK is missing: pip install 'dlboard[otel]'"
            raise ValueError(msg)
        if self.logs and _sdk_version() < _LOGS_MIN_SDK:
            msg = f"DLBOARD_TELEMETRY_LOGS needs opentelemetry-sdk>={_LOGS_MIN_SDK[0]}.{_LOGS_MIN_SDK[1]}, found {importlib.metadata.version('opentelemetry-sdk')}"
            raise ValueError(msg)
        return self


_LOGS_MIN_SDK: Final = (1, 38)
"""The first SDK whose `Logger.emit` takes a log record's fields as keyword arguments; metrics work with far older ones."""


def _sdk_version() -> tuple[int, int]:
    try:
        major, minor, *_ = importlib.metadata.version("opentelemetry-sdk").split(".")
    except (
        importlib.metadata.PackageNotFoundError
    ):  # importable but not installed as a distribution: take it as new enough
        return _LOGS_MIN_SDK
    return int(major), int(minor)


def _sdk_installed() -> bool:
    try:
        return importlib.util.find_spec("opentelemetry.sdk") is not None
    except (ImportError, ValueError):  # `opentelemetry` itself absent (or blocked)
        return False


class _Closeable(Protocol):
    def close(self) -> None: ...


_RETRY_FIRST_S: Final = 5.0
_RETRY_LAST_S: Final = 300.0


class _PerProcess[T: _Closeable]:
    """
    One lazily built `T` per process: `build` runs on the first `get()` after startup *or after a fork*.

    `build` returns `None` when its signal is switched off. A call that arrives on another thread
    mid-build also gets `None`, rather than waiting -- so does one that re-enters from the build
    itself (the warning it logs on failure goes through the log processor, which asks for `LOGS`).
    A build that fails is tried again after a back-off that doubles from 5 seconds to 5
    minutes, so a setup that was broken when the process started (a bad endpoint, a full disk)
    recovers on its own instead of costing the process its telemetry for good.
    """

    def __init__(self, build: Callable[[TelemetrySettings], T | None]) -> None:
        self._build = build
        self._reset()
        if sys.platform != "win32":
            # A lock another thread held at fork time would otherwise stay locked forever in the child.
            os.register_at_fork(after_in_child=self._reset)

    def _reset(self) -> None:
        self._lock = threading.Lock()
        self._pid: int | None = None
        self._value: T | None = None
        self._retry_at = 0.0
        self._backoff_s = _RETRY_FIRST_S

    def get(self) -> T | None:
        """The `T` for the current process, built on first use; `None` when switched off or not (yet) built."""
        if self._pid == os.getpid():
            return self._value
        if time.monotonic() < self._retry_at:
            return None
        with self._lock:
            if self._pid == os.getpid():
                return self._value
            if time.monotonic() < self._retry_at:  # another thread has just failed
                return None
            self._pid = os.getpid()
            try:
                self._value = self._build(TelemetrySettings())
            except Exception:  # noqa: BLE001 -- a down collector or bad endpoint must not take a request or startup with it
                self._pid = None  # not built: ask again once the back-off is over
                self._retry_at = time.monotonic() + self._backoff_s
                _log.warning(
                    "telemetry is not available in this process; trying again in %.0f s",
                    self._backoff_s,
                    exc_info=True,
                )
                self._backoff_s = min(self._backoff_s * 2, _RETRY_LAST_S)
                return None
            if self._value is not None:
                if self._backoff_s != _RETRY_FIRST_S:
                    _log.info("telemetry is available again in this process")
                    self._backoff_s = _RETRY_FIRST_S
                # Not `atexit`: a Granian worker exits through multiprocessing's `_exit_function`, which
                # runs these. 50 is below the blob store's 100, so its writer drains (and records its
                # last metrics) first.
                util.Finalize(None, self.close, exitpriority=50)
            return self._value

    def close(self) -> None:
        """Flush and shut down what this process built (a no-op in any other process), then forget it."""
        value = self._value if self._pid == os.getpid() else None
        self._reset()
        if value is not None:
            try:
                value.close()
            except Exception:  # noqa: BLE001 -- flushing on the way out must not turn a clean exit into a crash
                _log.warning("could not flush telemetry on exit", exc_info=True)


def _build_metrics(settings: TelemetrySettings) -> Metrics | None:
    if not settings.metrics:
        return None
    # Deferred: the SDK is only imported once telemetry is on.
    from dlboard.serve.telemetry._sdk import build_metrics

    return build_metrics(settings)


def _build_logs(settings: TelemetrySettings) -> Logs | None:
    if not settings.logs:
        return None
    from dlboard.serve.telemetry._sdk import (
        build_logs,
    )

    return build_logs(settings)


METRICS: Final = _PerProcess(_build_metrics)
LOGS: Final = _PerProcess(_build_logs)


class Unit(StrEnum):
    """A metric's unit, in UCUM: annotations in braces count things."""

    SECONDS = "s"
    BYTES = "By"
    REQUESTS = "{request}"
    VALUES = "{value}"
    ARTIFACTS = "{artifact}"
    UPLOADS = "{upload}"
    RUNS = "{run}"
    ATTEMPTS = "{attempt}"


SECONDS_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10)
"""Histogram bucket bounds for every `Unit.SECONDS` histogram -- the SDK's own defaults are sized for milliseconds."""


class AttributeKey(StrEnum):
    """
    The names dlboard's own measurements' labels are reported under: OpenTelemetry's semantic conventions where one exists.

    A plugin that records its own metrics defines a `StrEnum` of its own label names instead.
    """

    HTTP_REQUEST_METHOD = "http.request.method"
    HTTP_ROUTE = "http.route"
    HTTP_RESPONSE_STATUS_CODE = "http.response.status_code"
    RUN_STATUS = "dlboard.run.status"
    OUTCOME = "dlboard.outcome"
    REJECTION_REASON = "dlboard.rejection.reason"
    SIGN_IN_OUTCOME = "dlboard.auth.outcome"


_KEY: Final = "attribute_key"


def attribute(key: StrEnum) -> Any:  # noqa: ANN401 -- a dataclass `field()`, typed by the annotation of the field it sets
    """A field of an `Attributes` class that is reported under `key`: one of `AttributeKey`, or a `StrEnum` of your own."""
    return field(metadata={_KEY: key})


@dataclass(frozen=True, slots=True)
class Attributes:
    """
    The labels on one measurement. Subclass it (as a frozen dataclass) with a field per label, each set by `attribute`.

    A call site builds one, so a wrong label or a wrong type is caught by the type checker, and only
    an instrument that is actually recording turns it into the mapping OpenTelemetry wants.
    """

    def as_otel(self) -> dict[str, AttributeValue]:
        """The labels, under the names `attribute` gave them."""
        return {str(f.metadata[_KEY]): getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True, slots=True)
class NoAttributes(Attributes):
    """The labels of an instrument with a single series."""


class Outcome(StrEnum):
    """Whether an operation worked."""

    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True, slots=True)
class OutcomeAttributes(Attributes):
    """The labels of an instrument that counts or times something that can work or not."""

    outcome: Outcome = attribute(AttributeKey.OUTCOME)  # noqa: RUF009 -- the immutable call is configured by its qualified name, which is not this module's


class _Adder(Protocol):
    def add(self, amount: float, attributes: OtelAttributes = None) -> None: ...


class _Recorder(Protocol):
    def record(self, amount: float, attributes: OtelAttributes = None) -> None: ...


class _Instrument[I](ABC):
    """
    A named instrument that finds (and caches) its SDK counterpart on the current process's meter.

    Telemetry never takes down what it measures: whatever goes wrong inside `add`/`record` is
    logged, once per instrument, and swallowed.
    """

    def __init__(self, name: str, unit: Unit, description: str) -> None:
        self._name = name
        self._unit = unit
        self._description = description
        self._cache: tuple[Meter, I] | None = None
        self._warned = False

    @abstractmethod
    def _create(self, meter: Meter) -> I: ...

    def _resolve(self) -> I | None:
        metrics = METRICS.get()
        if metrics is None:
            return None
        # A different meter than last time means a fork, a rebuild, or a test reset.
        if self._cache is None or self._cache[0] is not metrics.meter:
            self._cache = (metrics.meter, self._create(metrics.meter))
        return self._cache[1]

    def _failed(self) -> None:
        if not self._warned:
            self._warned = True
            _log.warning(
                "could not record %s; further failures of it are not logged", self._name, exc_info=True
            )


class Counter[A: Attributes](_Instrument[_Adder]):
    """A total that only goes up."""

    @override
    def _create(self, meter: Meter) -> _Adder:
        return meter.create_counter(self._name, self._unit, self._description)

    def add(self, amount: int, attributes: A | None = None) -> None:
        """Count `amount` more, for the series `attributes` picks out."""
        try:
            if (instrument := self._resolve()) is not None:
                instrument.add(amount, None if attributes is None else attributes.as_otel())
        except Exception:  # noqa: BLE001 -- see the class docstring
            self._failed()


class UpDownCounter[A: Attributes](Counter[A]):
    """A running total that goes both ways (requests in flight, ...)."""

    @override
    def _create(self, meter: Meter) -> _Adder:
        return meter.create_up_down_counter(self._name, self._unit, self._description)


class Histogram[A: Attributes](_Instrument[_Recorder]):
    """The distribution of a measurement (a duration, ...)."""

    @override
    def _create(self, meter: Meter) -> _Recorder:
        return meter.create_histogram(self._name, self._unit, self._description)

    def record(self, amount: float, attributes: A | None = None) -> None:
        """Add one measurement to the series `attributes` picks out."""
        try:
            if (instrument := self._resolve()) is not None:
                instrument.record(amount, None if attributes is None else attributes.as_otel())
        except Exception:  # noqa: BLE001 -- see the class docstring
            self._failed()


@contextmanager
def shared_prometheus_dir() -> Generator[None]:
    """
    Give every process of this server run one directory to leave its Prometheus snapshots in.

    Set through the environment, which Granian's workers (and the processes they spawn) inherit.
    Always a fresh directory, removed afterwards -- inside `DLBOARD_TELEMETRY_PROMETHEUS_DIR` if that's
    set -- so nothing outlives the run and no leftover can be mistaken for a live process.
    """
    settings = TelemetrySettings()
    if MetricsExporter.PROMETHEUS not in settings.metrics:
        yield
        return
    try:
        path = tempfile.mkdtemp(prefix="dlboard-metrics-", dir=settings.prometheus_dir)
    except OSError:
        _log.warning(
            "no directory for Prometheus snapshots: /metrics will report one process at a time", exc_info=True
        )
        yield
        return
    set_setting_env(TelemetrySettings, "prometheus_dir", path)
    try:
        yield
    finally:
        shutil.rmtree(path, ignore_errors=True)


def register(app: Dash) -> None:
    """Serve `/metrics` -- public, so restrict it at the proxy -- when Prometheus is among the exporters."""
    if MetricsExporter.PROMETHEUS in TelemetrySettings().metrics:
        # Deferred: `_backend` imports this package, and Prometheus is optional.
        from dlboard.serve._backend._auth import add_public_route
        from dlboard.serve.telemetry._prometheus import serve_metrics

        add_public_route(app, "metrics", serve_metrics, ["GET"])


def _one(_item: object) -> int:
    return 1


class Tally[T]:
    """
    Counts what flows through an iterable as the handler consumes it: no second pass, nothing held on to.

    Wrap an iterable that is passed along lazily, and read `total` once it has been consumed. Counting
    happens on each item as it goes by, so it costs the handler nothing it was not doing, and, like
    every other instrument, it cannot change the handler's behaviour: if `weigh` fails, counting
    stops (with one warning) and the items keep flowing.
    """

    def __init__(self, items: Iterable[T], weigh: Callable[[T], int] = _one) -> None:
        """Count the items of `items`, each as many as `weigh` says (one, by default)."""
        self._items = items
        self._weigh = weigh
        self._broken = False
        self.total = 0
        """What has been counted so far."""

    def __iter__(self) -> Iterator[T]:
        """Pass the items on, counting each as it goes by."""
        for item in self._items:
            if not self._broken:
                try:
                    self.total += self._weigh(item)
                except Exception:  # noqa: BLE001 -- see the class docstring
                    self._broken = True
                    _log.warning("could not count an item; counting stops", exc_info=True)
            yield item
