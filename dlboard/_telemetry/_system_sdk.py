"""
The OpenTelemetry half of `dlboard._telemetry.system` -- imported only once a run has something to sample.

Every reading is an observable gauge on a `MeterProvider` of the run's own, read by an `InMemoryMetricReader` on the
sampler's timer: the provider is never made the global one, and nothing is exported anywhere. Names follow
OpenTelemetry's semantic conventions (`system.*`, `process.*`, `hw.gpu.*`) where they have one. Calls only API in the
oldest SDK the client's extras allow (1.16), as `_sdk.py` does.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeAlias, cast

from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Gauge, InMemoryMetricReader
from pydantic_settings import BaseSettings
from structlog.stdlib import get_logger

from dlboard.models import SYSTEM_METRIC_PREFIX

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from pynvml import c_nvmlDevice_t

_log = get_logger(__name__)

_STOP_WAIT_SEC: Final = 1.0
"""How long `stop` waits for a sample in progress: a hung driver call must not hold up the run's end."""

Reading: TypeAlias = "list[tuple[float, dict[str, str]]]"
"""A gauge's values at one sample, each with the attributes that tell it apart (which GPU)."""


@dataclass(frozen=True)
class _Gauge:
    name: str
    unit: str
    read: Callable[[], Reading]

    def observe(self, options: CallbackOptions) -> Iterable[Observation]:
        del options
        return [Observation(value, attributes) for value, attributes in self.read()]


def _host_gauges() -> list[_Gauge]:
    """The host's CPU and memory, and the training process's own (this one: it samples itself), through psutil."""
    import psutil

    process = psutil.Process()
    cpus = psutil.cpu_count() or 1
    # The first reading of each is measured from nothing; this one makes the first real sample cover its interval.
    psutil.cpu_percent()
    process.cpu_percent()

    def host_cpu() -> Reading:
        return [(psutil.cpu_percent() / 100, {})]

    def host_memory() -> Reading:
        return [(float(psutil.virtual_memory().used), {})]

    def host_memory_share() -> Reading:
        return [(psutil.virtual_memory().percent / 100, {})]

    def process_cpu() -> Reading:
        return [(process.cpu_percent() / 100 / cpus, {})]

    def process_memory() -> Reading:
        return [(float(process.memory_info().rss), {})]

    return [
        _Gauge("system.cpu.utilization", "1", host_cpu),
        _Gauge("system.memory.usage", "By", host_memory),
        _Gauge("system.memory.utilization", "1", host_memory_share),
        _Gauge("process.cpu.utilization", "1", process_cpu),
        _Gauge("process.memory.usage", "By", process_memory),
    ]


class _CudaEnv(BaseSettings):
    """Which GPUs the process may use, as CUDA reads it: NVML itself ignores it, and lists every GPU on the host."""

    cuda_visible_devices: str | None = None


@dataclass
class _Nvml:
    """NVML opened, and the run's own GPUs in the order CUDA numbers them (`cuda:0` first)."""

    handles: list[c_nvmlDevice_t]

    @classmethod
    def open(cls) -> _Nvml | None:
        """NVML, or `None` where it can't be used (no NVIDIA driver, no GPU, a container without the library)."""
        import pynvml

        try:
            pynvml.nvmlInit()
            return cls(_visible_handles())
        except pynvml.NVMLError as exc:
            _log.info("No GPU metrics for this run: NVML can't be used here", reason=str(exc))
            return None

    def gauges(self) -> list[_Gauge]:
        import pynvml

        def per_gpu(read: Callable[[c_nvmlDevice_t], float]) -> Callable[[], Reading]:
            def reading() -> Reading:
                values: Reading = []
                for index, handle in enumerate(self.handles):
                    try:
                        values.append((read(handle), {"hw.id": f"gpu{index}"}))
                    except pynvml.NVMLError:  # a reading this GPU doesn't support: left out, not zero
                        continue
                return values

            return reading

        def utilization(handle: c_nvmlDevice_t) -> float:
            return float(pynvml.nvmlDeviceGetUtilizationRates(handle).gpu) / 100

        def memory(handle: c_nvmlDevice_t) -> float:
            return float(pynvml.nvmlDeviceGetMemoryInfo(handle).used)

        def memory_share(handle: c_nvmlDevice_t) -> float:
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            return float(info.used) / float(info.total)

        def temperature(handle: c_nvmlDevice_t) -> float:
            return float(pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU))

        def power(handle: c_nvmlDevice_t) -> float:
            return pynvml.nvmlDeviceGetPowerUsage(handle) / 1000  # milliwatts

        return [
            _Gauge("hw.gpu.utilization", "1", per_gpu(utilization)),
            _Gauge("hw.gpu.memory.usage", "By", per_gpu(memory)),
            _Gauge("hw.gpu.memory.utilization", "1", per_gpu(memory_share)),
            _Gauge("hw.gpu.temperature", "Cel", per_gpu(temperature)),
            _Gauge("hw.gpu.power", "W", per_gpu(power)),
        ]

    def close(self) -> None:
        import pynvml

        try:
            pynvml.nvmlShutdown()
        except pynvml.NVMLError:
            return


def _visible_handles() -> list[c_nvmlDevice_t]:
    """
    The GPUs `CUDA_VISIBLE_DEVICES` lets the process use, in its order; every GPU when it isn't set.

    An entry is an index or a UUID (`GPU-...`, `MIG-...`). As CUDA does, the list stops at the first entry that names
    no GPU, so `-1` (the usual "no GPU") leaves none.
    """
    import pynvml

    visible = _CudaEnv().cuda_visible_devices
    if visible is None:
        return [pynvml.nvmlDeviceGetHandleByIndex(index) for index in range(pynvml.nvmlDeviceGetCount())]
    handles: list[c_nvmlDevice_t] = []
    for entry in (part.strip() for part in visible.split(",")):
        try:
            if entry.isdigit():
                handles.append(pynvml.nvmlDeviceGetHandleByIndex(int(entry)))
            elif entry.startswith(("GPU-", "MIG-")):
                # Untyped, and pyrefly infers `str` from its source: it is the same handle `ByIndex` returns.
                handles.append(cast("c_nvmlDevice_t", pynvml.nvmlDeviceGetHandleByUUID(entry)))
            else:
                break
        except pynvml.NVMLError:
            break
    return handles


def _key(name: str, attributes: Mapping[str, object]) -> str:
    """`system/cpu.utilization`, `system/hw.gpu.utilization/gpu0`: the run's metric key for one gauge's value."""
    return SYSTEM_METRIC_PREFIX + "/".join(
        [name.removeprefix("system."), *(str(v) for v in attributes.values())]
    )


class SystemSampler:
    """Samples the gauges into `log` every `interval_s`, on a daemon thread, until `stop`."""

    def __init__(
        self,
        interval_s: float,
        log: Callable[[Mapping[str, float]], None],
        gauges: list[_Gauge],
        nvml: _Nvml | None,
    ) -> None:
        self._interval_s, self._log, self._nvml = interval_s, log, nvml
        self._reader = InMemoryMetricReader()
        self._provider = MeterProvider(metric_readers=[self._reader], shutdown_on_exit=False)
        meter = self._provider.get_meter("dlboard.system")
        for gauge in gauges:
            meter.create_observable_gauge(gauge.name, callbacks=[gauge.observe], unit=gauge.unit)
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._sample_until_stopped, name="dlboard-system-metrics", daemon=True
        )

    @classmethod
    def start(
        cls, interval_s: float, log: Callable[[Mapping[str, float]], None], *, cpu: bool, gpu: bool
    ) -> SystemSampler | None:
        """Start sampling what can be read here, or `None` if nothing can (`gpu` alone, on a machine NVML can't use)."""
        nvml = _Nvml.open() if gpu else None
        gauges = [*(_host_gauges() if cpu else []), *(nvml.gauges() if nvml is not None else [])]
        if not gauges:
            return None
        sampler = cls(interval_s, log, gauges, nvml)
        sampler._thread.start()
        return sampler

    def sample(self) -> dict[str, float]:
        """Every gauge's value now, by the run's metric key."""
        data = self._reader.get_metrics_data()
        return {
            _key(metric.name, point.attributes or {}): float(point.value)
            for resource in (data.resource_metrics if data is not None else [])
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if isinstance(metric.data, Gauge)
            for point in metric.data.data_points
        }

    def _sample_until_stopped(self) -> None:
        while not self._stopped.wait(self._interval_s):
            try:
                values = self.sample()
            except Exception:  # noqa: BLE001 -- a sample that fails (a driver error) must never end training
                _log.warning("A system metrics sample failed; the next one is tried as usual", exc_info=True)
                continue
            if values:
                self._log(values)

    def stop(self) -> None:
        """Stop sampling, then let go of the provider and NVML: once, however often it is asked (a run's finish, then its exit)."""
        if self._stopped.is_set():
            return
        self._stopped.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(_STOP_WAIT_SEC)
        self._provider.shutdown()
        if self._nvml is not None:
            self._nvml.close()
