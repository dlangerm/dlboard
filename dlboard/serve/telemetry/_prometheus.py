"""
A `/metrics` that adds up every worker, for a Prometheus scrape that lands on any one of them.

Granian runs several worker processes (and each has a blob-writer process), each counting only what
it saw itself. Every process periodically writes its metrics, in Prometheus text format, to its own
file in a directory they all share (`shared_prometheus_dir`); whichever process answers a scrape
adds those files up with its own live numbers. Other processes' numbers are therefore up to one
snapshot interval old.

The directory never holds more than one file per live process plus `retired.prom`. When a process
dies its file is folded into `retired.prom` -- by the process itself on a clean exit, or by whichever
worker next serves `/metrics` after a crash. Its counters and histograms are kept (dropping them
would make the total fall, which `rate()` reads as a reset and answers with a huge spike); its gauges
are not, since an up/down count of a dead process's requests in flight means nothing.

Nothing here may take the server down: a snapshot that can't be written or read is logged and
skipped, and a scrape that can't add everything up falls back to this process's own numbers.
"""

from __future__ import annotations

import fcntl
import os
import threading
from contextlib import contextmanager
from http import HTTPStatus
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from flask import Response
from prometheus_client import GC_COLLECTOR, PLATFORM_COLLECTOR, PROCESS_COLLECTOR, REGISTRY, CollectorRegistry
from prometheus_client.core import Metric
from prometheus_client.exposition import CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.parser import text_string_to_metric_families
from prometheus_client.registry import Collector
from structlog.stdlib import get_logger

from dlboard.serve.telemetry import METRICS, TelemetrySettings

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable
    from pathlib import Path

_log = get_logger(__name__)

_RETIRED: Final = "retired.prom"
_FOLDED: Final = "# folded "
"""Marks a snapshot already added to `retired.prom`, in case its process died before deleting the file."""
_PER_PROCESS: Final = frozenset(
    {m.name for c in (PROCESS_COLLECTOR, PLATFORM_COLLECTOR, GC_COLLECTOR) for m in c.collect()}
    | {"target", "target_info"}
)
"""What `prometheus_client` and the OTel exporter add about each process itself -- meaningless to add up. (The exporter's info metric is `target` in memory and `target_info` once parsed back from text.)"""
_COLLECT_LOCK: Final = threading.Lock()
"""
Keeps the snapshot thread and a scrape in this process from collecting at once.

A thread lock is the right kind: it protects the OTel reader's in-memory collector, which exists once
per process. What the *processes* share is the directory, and `_locked` is what guards that.
"""


class _Fixed(Collector):
    def __init__(self, families: Iterable[Metric]) -> None:
        self._families = list(families)

    def collect(self) -> Iterable[Metric]:
        return self._families


def _render(families: Iterable[Metric]) -> bytes:
    registry = CollectorRegistry()
    registry.register(_Fixed(families))
    return generate_latest(registry)


def _own_families() -> list[Metric]:
    """The live numbers of the current process: what the OTel reader registered, minus the per-process extras."""
    with _COLLECT_LOCK:
        return [m for m in REGISTRY.collect() if m.name not in _PER_PROCESS]


def _read(path: Path, *, live: bool) -> list[Metric]:
    """
    The families in the snapshot at `path`, or none if it is gone or unreadable. A dead process's lose their gauges.

    A file that won't parse can't be repaired (it is only ever written whole, by `_atomic_write`), so it
    is skipped rather than allowed to fail every scrape from now on.
    """
    try:
        return [f for f in text_string_to_metric_families(path.read_text()) if live or f.type != "gauge"]
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001 -- see above
        _log.warning("ignoring the unreadable Prometheus snapshot %s", path, exc_info=True)
        return []


def _sum(families: Iterable[Metric]) -> list[Metric]:
    """One family per name, each sample the total over every input with the same name and labels."""
    merged: dict[str, Metric] = {}
    totals: dict[str, dict[tuple[str, tuple[tuple[str, str], ...]], float]] = {}
    for family in families:
        merged.setdefault(family.name, Metric(family.name, family.documentation, family.type, family.unit))
        into = totals.setdefault(family.name, {})
        for sample in family.samples:
            key = (sample.name, tuple(sorted(sample.labels.items())))
            into[key] = into.get(key, 0.0) + sample.value
    for name, metric in merged.items():
        for (sample_name, labels), value in totals[name].items():
            metric.add_sample(sample_name, dict(labels), value)
    return list(merged.values())


@contextmanager
def _locked(directory: Path) -> Generator[None]:
    """
    Hold the directory to ourselves: a fold and a scrape must not see each other half done.

    `flock` is atomic across processes. Each call opens its own file, and a lock belongs to the open
    file rather than the process, so threads of one process exclude each other too.
    """
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _atomic_write(path: Path, data: bytes) -> None:
    """Write `path` so that a reader sees all of the old contents or all of the new, never part of either."""
    staging = path.with_suffix(".tmp")
    staging.write_bytes(data)
    staging.replace(path)


def _token(path: Path) -> str:
    return path.name.split(".")[1]


def _fold(directory: Path, dead: Iterable[Path]) -> None:
    """Add each `dead` snapshot to `retired.prom` (under `_locked`) and delete it."""
    dead = list(dead)
    retired = directory / _RETIRED
    text = retired.read_text() if retired.exists() else ""
    folded = {line.removeprefix(_FOLDED).strip() for line in text.splitlines() if line.startswith(_FOLDED)}
    fresh = [p for p in dead if _token(p) not in folded]
    if fresh:
        families = _sum([*_read(retired, live=True), *(f for p in fresh for f in _read(p, live=False))])
        # A token is only needed while its file might still exist, so it lives until the next fold after that.
        keep = sorted({_token(p) for p in directory.glob("*.prom") if _token(p) in folded or p in fresh})
        _atomic_write(retired, _render(families) + "".join(f"{_FOLDED}{t}\n" for t in keep).encode())
    for path in dead:
        path.unlink(missing_ok=True)


def _is_dead(path: Path, own: Path | None) -> bool:
    """A snapshot whose process is gone: no such pid, or this pid's earlier life (so not `own`)."""
    pid = int(path.name.split(".")[0])
    if pid == os.getpid():
        return path != own
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass  # someone else's process, but a live one
    return False


class Snapshots:
    """The current process's snapshot file, rewritten on a timer."""

    def __init__(self, directory: Path, interval_s: float) -> None:
        self._directory = directory
        self.path = directory / f"{os.getpid()}.{uuid4().hex}.prom"
        self._stop = threading.Event()
        self._interval_s = interval_s
        self._thread = threading.Thread(target=self._run, daemon=True, name="telemetry-prometheus-snapshot")
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                _atomic_write(self.path, _render(_own_families()))
            except Exception:  # noqa: BLE001 -- a full disk must not end the thread; the next tick retries
                _log.warning("could not write the Prometheus snapshot %s", self.path, exc_info=True)

    def close(self) -> None:
        """Stop writing, then retire the current process's final numbers into `retired.prom`."""
        self._stop.set()
        self._thread.join()
        _atomic_write(self.path, _render(_own_families()))
        with _locked(self._directory):
            _fold(self._directory, [self.path])


def _merged() -> list[Metric]:
    """Every live process's numbers, plus every retired one's counters, summed."""
    metrics = METRICS.get()
    own = metrics.snapshots.path if metrics is not None and metrics.snapshots is not None else None
    directory = TelemetrySettings().prometheus_dir
    if directory is None:  # not run by `dlboard serve`: only this process can be asked
        return _sum(_own_families())
    with _locked(directory):
        others = [p for p in directory.glob("*.prom") if p.name != _RETIRED and p != own]
        _fold(directory, [p for p in others if _is_dead(p, own)])
        for leftover in directory.glob("*.tmp"):  # a process that died halfway through writing its snapshot
            snapshot = leftover.with_suffix(".prom")
            if snapshot.name != _RETIRED and _is_dead(snapshot, own):
                leftover.unlink(missing_ok=True)
        return _sum(
            [
                *_own_families(),
                *(f for p in others for f in _read(p, live=True)),
                *_read(directory / _RETIRED, live=True),
            ]
        )


def serve_metrics() -> Response:
    """Every process's numbers if they can all be added up, else the current process's alone, else a 503."""
    for gather in (_merged, lambda: _sum(_own_families())):
        try:
            return Response(_render(gather()), headers={"Content-Type": CONTENT_TYPE_LATEST})
        except Exception:  # noqa: BLE001 -- a scrape must degrade, never fail the process
            _log.warning("could not serve /metrics in full", exc_info=True)
    return Response(status=HTTPStatus.SERVICE_UNAVAILABLE)
