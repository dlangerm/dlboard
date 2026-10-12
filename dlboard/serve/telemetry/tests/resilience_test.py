"""Telemetry never takes the server down, and the processes sharing a snapshot directory never trip over each other."""

from __future__ import annotations

import subprocess
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
import structlog

from dlboard._mp_context import SPAWN_CONTEXT
from dlboard.conftest import scraped_total
from dlboard.serve.telemetry import METRICS, Counter, NoAttributes, Unit, _prometheus
from dlboard.serve.telemetry._prometheus import serve_metrics

if TYPE_CHECKING:
    from multiprocessing.synchronize import Barrier
    from pathlib import Path

_CALLS = Counter[NoAttributes]("test.calls", Unit.REQUESTS, "Calls made by a test.")
_SNAPSHOT = '# HELP test_widgets_total Widgets.\n# TYPE test_widgets_total counter\ntest_widgets_total{kind="a"} 1.0\n'
_PROCESSES = 4
_DEAD_SNAPSHOTS = 8


@pytest.fixture
def prometheus_dir(telemetry: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "prometheus")
    telemetry.setenv("DLBOARD_TELEMETRY_PROMETHEUS_DIR", str(tmp_path))
    return tmp_path


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def _scrape_at(barrier: Barrier) -> None:
    barrier.wait()  # all the processes scrape (and so fold) at the same moment
    serve_metrics()


def test_a_failing_instrument_warns_once_and_never_raises(telemetry: pytest.MonkeyPatch) -> None:
    def fail() -> None:
        msg = "meter provider on fire"
        raise RuntimeError(msg)

    telemetry.setattr(METRICS, "get", fail)

    with structlog.testing.capture_logs() as logs:
        _CALLS.add(1)
        _CALLS.add(1)

    assert [entry["log_level"] for entry in logs] == ["warning"]


def test_a_scrape_that_cannot_add_everything_up_serves_its_own_numbers(
    prometheus_dir: Path, telemetry: pytest.MonkeyPatch
) -> None:
    def fail() -> None:
        msg = "directory on fire"
        raise OSError(msg)

    _CALLS.add(3)
    telemetry.setattr(_prometheus, "_merged", fail)

    response = serve_metrics()

    assert response.status_code == HTTPStatus.OK
    assert scraped_total(response.get_data(as_text=True), "test_calls_total") == 3


def test_a_scrape_that_cannot_serve_anything_is_a_503_not_an_exception(telemetry: pytest.MonkeyPatch) -> None:
    def fail(*_args: object) -> None:
        msg = "everything on fire"
        raise OSError(msg)

    telemetry.setattr(_prometheus, "_merged", fail)
    telemetry.setattr(_prometheus, "_own_families", fail)

    assert serve_metrics().status_code == HTTPStatus.SERVICE_UNAVAILABLE


def test_a_corrupt_snapshot_is_skipped_and_does_not_poison_the_scrape(prometheus_dir: Path) -> None:
    _CALLS.add(2)
    (prometheus_dir / f"{_dead_pid()}.corrupt.prom").write_text("not { prometheus ] at all\n")

    scrape = serve_metrics().get_data(as_text=True)

    assert scraped_total(scrape, "test_calls_total") == 2
    assert not list(prometheus_dir.glob("*.corrupt.prom"))  # a dead process's, so folded away


def test_only_a_dead_processes_half_written_snapshot_is_swept(prometheus_dir: Path) -> None:
    _CALLS.add(1)
    own = METRICS.get()
    assert own is not None
    assert own.snapshots is not None
    mine = own.snapshots.path.with_suffix(".tmp")
    theirs = prometheus_dir / f"{_dead_pid()}.crashed.tmp"
    mine.write_text("being written right now")
    theirs.write_text("abandoned")

    serve_metrics()

    assert mine.exists()
    assert not theirs.exists()


def test_processes_scraping_at_once_fold_every_dead_snapshot_exactly_once(prometheus_dir: Path) -> None:
    pid = _dead_pid()
    for n in range(_DEAD_SNAPSHOTS):
        (prometheus_dir / f"{pid}.token{n}.prom").write_text(_SNAPSHOT)
    barrier = SPAWN_CONTEXT.Barrier(_PROCESSES)
    processes = [SPAWN_CONTEXT.Process(target=_scrape_at, args=(barrier,)) for _ in range(_PROCESSES)]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)

    assert [process.exitcode for process in processes] == [0] * _PROCESSES
    assert scraped_total(serve_metrics().get_data(as_text=True), "test_widgets_total") == _DEAD_SNAPSHOTS
    assert sorted(p.name for p in prometheus_dir.glob("*.prom")) == ["retired.prom"]
