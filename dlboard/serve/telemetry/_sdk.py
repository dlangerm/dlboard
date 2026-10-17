"""
The OpenTelemetry SDK half of `dlboard.serve.telemetry` -- imported only once telemetry is switched on.

Exporter endpoints, headers, protocol timeouts, temporality and the OTLP push interval are the SDK's
own `OTEL_EXPORTER_OTLP_*` / `OTEL_METRIC_EXPORT_INTERVAL` / `OTEL_RESOURCE_ATTRIBUTES` variables;
nothing here re-implements them.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import traceback
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

import pendulum
from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

from dlboard._telemetry._sdk import quiet_exporter_retries, resource
from dlboard._version import __version__
from dlboard.serve.telemetry import LOGS, SECONDS_BUCKETS, MetricsExporter, TelemetrySettings, Unit

if TYPE_CHECKING:
    from collections.abc import Mapping, MutableMapping

    from opentelemetry._logs import Logger
    from opentelemetry.metrics import Meter
    from opentelemetry.util.types import AttributeValue

    from dlboard.serve.telemetry._prometheus import Snapshots

_SERVICE_NAME: Final = "dlboard"


@dataclass(frozen=True)
class Metrics:
    """The current process's meter, plus the Prometheus snapshot writer if `/metrics` is being served."""

    meter: Meter
    provider: MeterProvider
    snapshots: Snapshots | None

    def close(self) -> None:
        """Write the last Prometheus snapshot, then flush and shut down every exporter."""
        try:
            if self.snapshots is not None:
                self.snapshots.close()  # before the shutdown below: that unregisters what it collects
        finally:
            self.provider.shutdown()


@dataclass(frozen=True)
class Logs:
    """The current process's OTLP logger."""

    provider: LoggerProvider
    logger: Logger

    def close(self) -> None:
        """Flush the batch of logs still waiting and shut down."""
        self.provider.shutdown()


def build_metrics(settings: TelemetrySettings) -> Metrics:
    """The meter provider with one reader per exporter in `settings.metrics`."""
    quiet_exporter_retries()
    readers: list[MetricReader] = []
    for exporter in sorted(settings.metrics):
        match exporter:
            case MetricsExporter.OTLP:
                readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
            case MetricsExporter.PROMETHEUS:
                readers.append(PrometheusMetricReader())
    provider = MeterProvider(
        resource=resource(os.getpid(), _SERVICE_NAME),
        metric_readers=readers,
        # The SDK's default buckets are sized for milliseconds; every duration here is in seconds.
        views=[
            View(
                instrument_type=Histogram,
                instrument_unit=Unit.SECONDS,
                aggregation=ExplicitBucketHistogramAggregation(SECONDS_BUCKETS),
            )
        ],
        shutdown_on_exit=False,  # `PerProcess` closes it, where a Granian worker's exit actually runs
    )
    snapshots = None
    if MetricsExporter.PROMETHEUS in settings.metrics and settings.prometheus_dir is not None:
        from dlboard.serve.telemetry._prometheus import Snapshots

        snapshots = Snapshots(settings.prometheus_dir, settings.prometheus_snapshot_s)
    return Metrics(provider.get_meter(_SERVICE_NAME, __version__), provider, snapshots)


def build_logs(_settings: TelemetrySettings) -> Logs:
    """The logger provider that batches log records and pushes them over OTLP."""
    quiet_exporter_retries()
    provider = LoggerProvider(resource=resource(os.getpid(), _SERVICE_NAME), shutdown_on_exit=False)
    provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    return Logs(provider, provider.get_logger(_SERVICE_NAME, __version__))


class _LogLevel(StrEnum):
    """The levels structlog's `add_log_level` produces."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


def _severity(level: str) -> SeverityNumber:
    try:
        match _LogLevel(level):
            case _LogLevel.DEBUG:
                return SeverityNumber.DEBUG
            case _LogLevel.INFO:
                return SeverityNumber.INFO
            case _LogLevel.WARNING:
                return SeverityNumber.WARN
            case _LogLevel.ERROR:
                return SeverityNumber.ERROR
            case _LogLevel.CRITICAL:
                return SeverityNumber.FATAL
    except ValueError:  # a custom level
        return SeverityNumber.UNSPECIFIED


_NOT_ATTRIBUTES: Final = frozenset(
    {"event", "level", "timestamp", "exc_info", "stack", "trace_id", "span_id"}
)
"""What an event carries that is not an attribute of its log record. A span's ids are the record's own fields, filled from the open span."""


def _emit(logger: Logger, event_dict: Mapping[str, object]) -> None:
    """Send one structlog event as an OpenTelemetry log record."""
    level = str(event_dict.get("level", ""))
    attributes: dict[str, AttributeValue] = {
        k: v if isinstance(v, str | bool | int | float) else str(v)
        for k, v in event_dict.items()
        if k not in _NOT_ATTRIBUTES
    }
    # Spelled out as the semconv `exception.*` attributes; `format_exception` never includes locals.
    exc: BaseException | None
    match event_dict.get("exc_info"):
        case True:
            exc = sys.exc_info()[1]
        case (BaseException() as raised) | (_, BaseException() as raised, _):
            exc = raised
        case _:
            exc = None
    if exc is not None:
        attributes["exception.type"] = type(exc).__qualname__
        attributes["exception.message"] = str(exc)
        attributes["exception.stacktrace"] = "".join(traceback.format_exception(exc))
    now = pendulum.now("UTC")
    nanoseconds = now.int_timestamp * 1_000_000_000 + now.microsecond * 1_000
    logger.emit(
        timestamp=nanoseconds,
        observed_timestamp=nanoseconds,
        severity_text=level.upper(),
        severity_number=_severity(level),
        body=str(event_dict.get("event", "")),
        attributes=attributes,
    )


_WARNED: Final = threading.Event()
"""Set once a failure to forward has been reported, so a persistent one is not reported per log line."""


def forward_log(
    _logger: object, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """
    A structlog processor: send each event over OTLP, and pass it on to the console/JSON renderer unchanged.

    Logging must never fail because telemetry did, so nothing in here may raise.
    """
    try:
        if (logs := LOGS.get()) is not None:
            _emit(logs.logger, event_dict)
    except Exception:
        if not _WARNED.is_set():
            _WARNED.set()
            # The standard library's logger, not structlog's: a failure of *this* processor must not recurse into it.
            logging.getLogger(__name__).exception(
                "could not send a log event over OTLP; further failures are not logged"
            )
    return event_dict
