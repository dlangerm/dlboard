"""
The OpenTelemetry SDK half of `dlboard.serve.telemetry` -- imported only once telemetry is switched on.

Exporter endpoints, headers, protocol timeouts, temporality and the OTLP push interval are the SDK's
own `OTEL_EXPORTER_OTLP_*` / `OTEL_METRIC_EXPORT_INTERVAL` / `OTEL_RESOURCE_ATTRIBUTES` variables;
nothing here re-implements them.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard._version import __version__
from dlboard.serve.telemetry import SECONDS_BUCKETS, MetricsExporter, TelemetrySettings, Unit

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter

    from dlboard.serve.telemetry._prometheus import Snapshots

_SERVICE_NAME: Final = "dlboard"


class OtelResourceSettings(BaseSettings):
    """The standard `OTEL_SERVICE_NAME`, which names the service in everything dlboard exports."""

    model_config = SettingsConfigDict(env_prefix="OTEL_")

    service_name: str = _SERVICE_NAME
    """The `service.name` every metric and log is reported under."""


@cache
def _resource(pid: int) -> Resource:  # noqa: ARG001 -- the pid is the cache key: one identity per process, shared by metrics and logs
    """
    Who is exporting: a `service.instance.id` unique to this process.

    Every worker pushes its own cumulative counts, so they must not share an identity or a collector
    would see one series jumping around. Set in code, so it overrides `OTEL_RESOURCE_ATTRIBUTES`.
    """
    # A down collector retries quietly; only the final "failed to export" error is worth a line.
    logging.getLogger("opentelemetry").setLevel(logging.ERROR)
    return Resource.create(
        {
            "service.name": OtelResourceSettings().service_name,
            "service.instance.id": uuid4().hex,
            "service.version": __version__,
        }
    )


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


def build_metrics(settings: TelemetrySettings) -> Metrics:
    """The meter provider with one reader per exporter in `settings.metrics`."""
    readers: list[MetricReader] = []
    for exporter in sorted(settings.metrics):
        match exporter:
            case MetricsExporter.OTLP:
                readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
            case MetricsExporter.PROMETHEUS:
                readers.append(PrometheusMetricReader())
    provider = MeterProvider(
        resource=_resource(os.getpid()),
        metric_readers=readers,
        # The SDK's default buckets are sized for milliseconds; every duration here is in seconds.
        views=[
            View(
                instrument_type=Histogram,
                instrument_unit=Unit.SECONDS,
                aggregation=ExplicitBucketHistogramAggregation(SECONDS_BUCKETS),
            )
        ],
        shutdown_on_exit=False,  # `_PerProcess` closes it, where a Granian worker's exit actually runs
    )
    snapshots = None
    if MetricsExporter.PROMETHEUS in settings.metrics and settings.prometheus_dir is not None:
        from dlboard.serve.telemetry._prometheus import Snapshots

        snapshots = Snapshots(settings.prometheus_dir, settings.prometheus_snapshot_s)
    return Metrics(provider.get_meter(_SERVICE_NAME, __version__), provider, snapshots)
