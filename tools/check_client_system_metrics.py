"""
Prove a run's system metrics sample with whichever OpenTelemetry SDK, psutil and NVML binding are installed -- run by CI.

`client/pyproject.toml`'s `system` and `gpu` extras promise floors (and no ceilings), as `otel` does: CI installs exactly
them, then the newest, and samples once through the installed client wheel, as `tools/check_client_tracing.py` does for
traces.

    uv pip install --python venv/bin/python --resolution lowest-direct -r client/pyproject.toml --extra system
    uv pip install --python venv/bin/python --no-deps dlboard_client-X.whl
    venv/bin/python tools/check_client_system_metrics.py system

`gpu` checks the `gpu` extra instead. CI has no GPU, so there it proves what it can without one: every `pynvml` name the
sampler calls exists in the installed binding, and a run asking for GPU metrics alone on a machine NVML can't use gets no
sampler rather than an error. On a machine with a GPU it samples one, too.
"""

from __future__ import annotations

import inspect
import re
import sys

from dlboard._compat import StrEnum
from dlboard._telemetry import _system_sdk
from dlboard._telemetry._system_sdk import SystemSampler


class Extra(StrEnum):
    """The client extra to check, named on the command line."""

    SYSTEM = "system"
    GPU = "gpu"


def _sample(extra: Extra) -> dict[str, float] | None:
    """One sample of `extra`'s gauges alone, or `None` if nothing here can be read."""
    sampler = SystemSampler.start(3600.0, lambda _: None, cpu=extra is Extra.SYSTEM, gpu=extra is Extra.GPU)
    if sampler is None:
        return None
    try:
        return sampler.sample()
    finally:
        sampler.stop()


def _check_system() -> None:
    sampled = _sample(Extra.SYSTEM)
    if sampled is None:
        sys.exit("nothing to sample with psutil installed")
    expected = {
        "system/cpu.utilization",
        "system/memory.usage",
        "system/memory.utilization",
        "system/process.cpu.utilization",
        "system/process.memory.usage",
    }
    if missing := expected - set(sampled):
        sys.exit(f"not sampled: {sorted(missing)}")
    if not 0 <= sampled["system/cpu.utilization"] <= 1 or sampled["system/process.memory.usage"] <= 0:
        sys.exit(f"out of range: {sampled}")
    sys.stdout.write("system metrics sampled\n")


def _check_gpu() -> None:
    import pynvml

    called = set(re.findall(r"pynvml\.(\w+)", inspect.getsource(_system_sdk)))
    if missing := sorted(name for name in called if not hasattr(pynvml, name)):
        sys.exit(f"not in this nvidia-ml-py: {missing}")
    sampled = _sample(Extra.GPU)
    if sampled is None:
        sys.stdout.write(
            f"{len(called)} NVML calls found; no GPU here, and the run got no sampler rather than an error\n"
        )
        return
    if "system/hw.gpu.memory.usage/gpu0" not in sampled:
        sys.exit(f"no GPU memory sampled: {sampled}")
    sys.stdout.write("gpu metrics sampled\n")


def main() -> None:
    """Check the extra named on the command line, and say what is missing or out of range."""
    match Extra(sys.argv[1]):
        case Extra.SYSTEM:
            _check_system()
        case Extra.GPU:
            _check_gpu()


if __name__ == "__main__":
    main()
