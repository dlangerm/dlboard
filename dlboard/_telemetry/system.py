"""
A run's system metrics: the host's CPU and memory, the training process's own, and each GPU the run can see.

Sampled in the training process (process metrics have to describe it) every `interval_s`, on a timer thread of their
own, and logged into the run as ordinary metrics under `models.SYSTEM_METRIC_PREFIX`. They are read through
OpenTelemetry instruments on a meter provider of dlboard's own (`_system_sdk.py`), never the global one -- the training
process is the user's -- so there is one way of collecting telemetry, and nothing leaves the process except to the
dlboard server.

Two extras, apart because NVML is missing or unusable on plenty of machines that still have a CPU and memory worth
watching: `dlboard-client[system]` (psutil) and `dlboard-client[gpu]` (NVML). Each part is on when its extra is
installed; without either, or without the OpenTelemetry SDK, nothing here imports anything.
"""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING, Protocol

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


class SystemMetricsSettings(BaseSettings):
    """Whether, and how often, a run samples its system metrics (`DLBOARD_SYSTEM_METRICS_*`)."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_SYSTEM_METRICS_", frozen=True)

    enabled: bool = True
    """Sample whatever the installed extras can read: `[system]` for CPU and memory, `[gpu]` for NVIDIA GPUs."""
    interval_s: float = Field(default=30.0, ge=1.0)
    """Seconds between samples. Each is a handful of values per GPU: lower costs rows, not training time."""
    gpu: bool = True
    """Sample NVIDIA GPUs through NVML, when the `[gpu]` extra is installed."""


class Sampler(Protocol):
    """Samples on its own thread until stopped."""

    def stop(self) -> None:
        """Stop sampling, and let go of what was opened to sample (NVML)."""
        ...


def _installed(module: str) -> bool:
    """Whether `module` can be imported, found without importing it (a dotted name's parent package is imported)."""
    try:
        return importlib.util.find_spec(module) is not None
    except ImportError:  # a dotted name whose parent package isn't installed
        return False


def start(settings: SystemMetricsSettings, log: Callable[[Mapping[str, float]], None]) -> Sampler | None:
    """
    Start sampling into `log` (a run's metrics, by key), or `None`: turned off, or no extra installed to sample with.

    Imports the SDK only when there is something to sample, so a client installed without these extras loads nothing.
    """
    cpu, gpu = _installed("psutil"), settings.gpu and _installed("pynvml")
    if not settings.enabled or not (cpu or gpu) or not _installed("opentelemetry.sdk"):
        return None
    from dlboard._telemetry._system_sdk import (
        SystemSampler,
    )

    return SystemSampler.start(settings.interval_s, log, cpu=cpu, gpu=gpu)
