"""A run's system metrics: real psutil readings, and NVML through a fake one (a test box rarely has an NVIDIA GPU)."""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import pytest

from dlboard._telemetry import system
from dlboard._telemetry._system_sdk import SystemSampler
from dlboard._telemetry.system import SystemMetricsSettings

if TYPE_CHECKING:
    from collections.abc import Mapping

_HOST_KEYS: Final = {
    "system/cpu.utilization",
    "system/memory.usage",
    "system/memory.utilization",
    "system/process.cpu.utilization",
    "system/process.memory.usage",
}


class _NVMLError(Exception):
    pass


@dataclass
class _Gpu:
    uuid: str
    utilization: int
    used: int
    total: int = 1000
    power_mw: int | None = 100_000
    """`None`: this GPU can't report its power, as some don't."""


@dataclass
class _Rates:
    gpu: int


@dataclass
class _Memory:
    used: int
    total: int


class _FakeNvml:
    """The few NVML calls the sampler makes, over a list of GPUs; `pynvml` is replaced by one of these."""

    NVMLError = _NVMLError
    NVML_TEMPERATURE_GPU = 0

    def __init__(self, gpus: list[_Gpu], *, usable: bool = True) -> None:
        self.gpus, self.usable, self.shut_down = gpus, usable, False

    def nvmlInit(self) -> None:  # noqa: N802 -- NVML's own names
        if not self.usable:
            msg = "Driver Not Loaded"
            raise _NVMLError(msg)

    def nvmlShutdown(self) -> None:  # noqa: N802
        self.shut_down = True

    def nvmlDeviceGetCount(self) -> int:  # noqa: N802
        return len(self.gpus)

    def nvmlDeviceGetHandleByIndex(self, index: int) -> _Gpu:  # noqa: N802
        if index >= len(self.gpus):
            raise _NVMLError(index)
        return self.gpus[index]

    def nvmlDeviceGetHandleByUUID(self, uuid: str) -> _Gpu:  # noqa: N802
        return next(gpu for gpu in self.gpus if gpu.uuid == uuid)

    def nvmlDeviceGetUtilizationRates(self, gpu: _Gpu) -> _Rates:  # noqa: N802
        return _Rates(gpu.utilization)

    def nvmlDeviceGetMemoryInfo(self, gpu: _Gpu) -> _Memory:  # noqa: N802
        return _Memory(gpu.used, gpu.total)

    def nvmlDeviceGetTemperature(self, gpu: _Gpu, sensor: int) -> int:  # noqa: N802
        del gpu, sensor
        return 60

    def nvmlDeviceGetPowerUsage(self, gpu: _Gpu) -> int:  # noqa: N802
        if gpu.power_mw is None:
            raise _NVMLError
        return gpu.power_mw


_GPUS: Final = [
    _Gpu("GPU-a", utilization=50, used=250),
    _Gpu("GPU-b", utilization=90, used=900, power_mw=None),
]


@pytest.fixture
def nvml(monkeypatch: pytest.MonkeyPatch) -> _FakeNvml:
    fake = _FakeNvml(list(_GPUS))
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    return fake


def _sampled(*, cpu: bool, gpu: bool) -> dict[str, float]:
    sampler = SystemSampler.start(3600.0, lambda _: None, cpu=cpu, gpu=gpu)
    assert sampler is not None
    try:
        return sampler.sample()
    finally:
        sampler.stop()


def test_the_host_and_the_training_process_are_sampled_through_psutil() -> None:
    sampled = _sampled(cpu=True, gpu=False)

    assert set(sampled) == _HOST_KEYS
    assert 0 <= sampled["system/cpu.utilization"] <= 1
    assert sampled["system/process.memory.usage"] > 0


def test_each_gpu_is_sampled_under_its_cuda_number_and_what_it_cannot_report_is_left_out(
    nvml: _FakeNvml,
) -> None:
    sampled = _sampled(cpu=False, gpu=True)

    assert sampled["system/hw.gpu.utilization/gpu0"] == pytest.approx(0.5)
    assert sampled["system/hw.gpu.memory.utilization/gpu1"] == pytest.approx(0.9)
    assert sampled["system/hw.gpu.power/gpu0"] == pytest.approx(100.0)
    assert "system/hw.gpu.power/gpu1" not in sampled
    assert nvml.shut_down


@pytest.mark.parametrize(
    ("visible", "utilization"),
    [("1", {"gpu0": 0.9}), ("GPU-b,GPU-a", {"gpu0": 0.9, "gpu1": 0.5}), ("0,7,1", {"gpu0": 0.5}), ("-1", {})],
    ids=["an-index", "uuids-in-their-order", "stops-at-one-that-names-no-gpu", "none"],
)
def test_only_the_gpus_cuda_lets_the_run_use_are_sampled(
    nvml: _FakeNvml, monkeypatch: pytest.MonkeyPatch, visible: str, utilization: Mapping[str, float]
) -> None:
    del nvml
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", visible)

    sampled = _sampled(cpu=True, gpu=True)

    assert {
        key.rsplit("/", 1)[1]: value
        for key, value in sampled.items()
        if key.startswith("system/hw.gpu.utilization/")
    } == pytest.approx(utilization)


def test_a_machine_nvml_cannot_use_still_samples_its_cpu_and_memory(nvml: _FakeNvml) -> None:
    nvml.usable = False

    assert set(_sampled(cpu=True, gpu=True)) == _HOST_KEYS
    assert SystemSampler.start(3600.0, lambda _: None, cpu=False, gpu=True) is None


def test_the_sampler_logs_every_interval_until_it_is_stopped(caplog: pytest.LogCaptureFixture) -> None:
    samples: list[Mapping[str, float]] = []
    two = threading.Event()

    def log(values: Mapping[str, float]) -> None:
        samples.append(values)
        if len(samples) >= 2:
            two.set()

    sampler = SystemSampler.start(0.05, log, cpu=True, gpu=False)
    assert sampler is not None
    assert two.wait(10)
    sampler.stop()
    stopped_at = len(samples)

    assert not sampler._thread.is_alive()
    assert len(samples) == stopped_at
    assert set(samples[0]) == _HOST_KEYS
    # A run stops it as it finishes, then again as it exits: the provider is shut down once, with nothing to say.
    with caplog.at_level(logging.WARNING):
        sampler.stop()
    assert caplog.records == []


def test_a_gpu_that_reports_no_memory_total_leaves_its_share_out(nvml: _FakeNvml) -> None:
    nvml.gpus = [_Gpu("GPU-z", utilization=10, used=0, total=0)]

    sampled = _sampled(cpu=False, gpu=True)

    assert "system/hw.gpu.memory.utilization/gpu0" not in sampled
    assert sampled["system/hw.gpu.utilization/gpu0"] == pytest.approx(0.1)


def test_a_sample_the_run_could_not_take_does_not_end_the_sampling() -> None:
    """`log` is the run's: whatever it raises is warned about, and the next sample is taken as usual."""
    calls = 0
    again = threading.Event()

    def log(values: Mapping[str, float]) -> None:
        nonlocal calls
        del values
        calls += 1
        if calls == 1:
            msg = "the run could not take it"
            raise RuntimeError(msg)
        again.set()

    sampler = SystemSampler.start(0.05, log, cpu=True, gpu=False)
    assert sampler is not None
    try:
        assert again.wait(10)
    finally:
        sampler.stop()


@pytest.mark.parametrize(
    ("settings", "installed", "samples"),
    [
        pytest.param(SystemMetricsSettings(), {"psutil", "opentelemetry.sdk"}, True, id="cpu"),
        pytest.param(
            SystemMetricsSettings(enabled=False), {"psutil", "opentelemetry.sdk"}, False, id="turned-off"
        ),
        pytest.param(SystemMetricsSettings(), {"opentelemetry.sdk"}, False, id="no-extra"),
        pytest.param(SystemMetricsSettings(), {"psutil"}, False, id="no-sdk"),
    ],
)
def test_a_run_samples_only_when_asked_and_an_extra_can(
    monkeypatch: pytest.MonkeyPatch, settings: SystemMetricsSettings, installed: set[str], *, samples: bool
) -> None:
    monkeypatch.setattr(system, "_installed", installed.__contains__)

    sampler: Any = system.start(settings, lambda _: None)

    assert (sampler is not None) is samples
    if sampler is not None:
        sampler.stop()
