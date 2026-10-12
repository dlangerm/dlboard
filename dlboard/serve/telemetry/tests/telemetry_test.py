"""
The per-process lazy setup and the merged `/metrics`, against the real SDK.

A fork is a real `os.fork()`, and a "dead" process is a real reaped pid, since pid handling is
the point. The sink-based OTLP checks are in `otlp_test.py`, the whole server's in
`dlboard/tests/telemetry_test.py`.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import TYPE_CHECKING

import pytest
import structlog

import dlboard.serve.telemetry as facade
from dlboard.conftest import scraped_total
from dlboard.serve.telemetry import (
    METRICS,
    Counter,
    NoAttributes,
    TelemetrySettings,
    Unit,
    _sdk,
)
from dlboard.serve.telemetry._prometheus import serve_metrics

if TYPE_CHECKING:
    from pathlib import Path

_CALLS = Counter[NoAttributes]("test.calls", Unit.REQUESTS, "Calls made by a test.")

_SNAPSHOT = """\
# HELP test_widgets_total Widgets.
# TYPE test_widgets_total counter
test_widgets_total{kind="a"} 3.0
# HELP test_in_flight Things in flight.
# TYPE test_in_flight gauge
test_in_flight 1.0
"""


@pytest.fixture
def prometheus_dir(telemetry: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "prometheus")
    telemetry.setenv("DLBOARD_TELEMETRY_PROMETHEUS_DIR", str(tmp_path))
    return tmp_path


def _scrape() -> str:
    return serve_metrics().get_data(as_text=True)


def test_nothing_is_built_while_telemetry_is_off() -> None:
    _CALLS.add(1)

    assert METRICS.get() is None


@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # fork() with the snapshot thread running
def test_a_forked_child_builds_its_own_provider_even_if_the_lock_was_held_at_fork(
    prometheus_dir: Path,
) -> None:
    parent = METRICS.get()
    assert parent is not None
    with METRICS._lock:
        pid = os.fork()
        if pid == 0:
            code = 1
            try:
                _CALLS.add(7)
                child = METRICS.get()
                code = 0 if child is not None and child.meter is not parent.meter else 1
                METRICS.close()
            finally:
                os._exit(code)
    _, status = os.waitpid(pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert scraped_total(_scrape(), "test_calls_total") == 7  # the child's count, once


def test_a_failing_build_warns_once_and_leaves_the_process_uninstrumented(
    telemetry: pytest.MonkeyPatch,
) -> None:
    def fail(_settings: object) -> None:
        msg = "collector unreachable"
        raise OSError(msg)

    telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "otlp")
    telemetry.setattr(_sdk, "build_metrics", fail)

    with structlog.testing.capture_logs() as logs:
        _CALLS.add(1)
        _CALLS.add(1)

    assert [entry["log_level"] for entry in logs] == ["warning"]


def test_a_cleanly_exiting_process_retires_its_numbers_and_leaves_no_snapshot(prometheus_dir: Path) -> None:
    _CALLS.add(5)

    METRICS.close()

    assert sorted(p.name for p in prometheus_dir.glob("*.prom")) == ["retired.prom"]
    assert "test_calls_total" in _scrape()


def test_a_scrape_adds_up_live_processes_and_retires_a_crashed_one(prometheus_dir: Path) -> None:
    live = subprocess.Popen(["sleep", "60"])
    dead = subprocess.Popen(["true"])
    dead.wait()
    try:
        (prometheus_dir / f"{live.pid}.live.prom").write_text(_SNAPSHOT)
        (prometheus_dir / f"{dead.pid}.dead.prom").write_text(_SNAPSHOT)

        first = _scrape()
        second = _scrape()
    finally:
        live.kill()
        live.wait()

    for scrape in (first, second):  # a second scrape counts nothing twice
        assert 'test_widgets_total{kind="a"} 6.0' in scrape  # the crashed process's counter is kept
        assert "test_in_flight 1.0" in scrape  # its gauge is not
    assert sorted(p.name for p in prometheus_dir.glob("*.prom")) == [f"{live.pid}.live.prom", "retired.prom"]


def test_a_snapshot_already_folded_before_a_crash_is_not_counted_twice(prometheus_dir: Path) -> None:
    dead = subprocess.Popen(["true"])
    dead.wait()
    (prometheus_dir / "retired.prom").write_text(_SNAPSHOT.replace("in_flight", "unused") + "# folded dead\n")
    (prometheus_dir / f"{dead.pid}.dead.prom").write_text(_SNAPSHOT)  # the process died before deleting it

    assert 'test_widgets_total{kind="a"} 3.0' in _scrape()
    assert [p.name for p in prometheus_dir.glob("*.prom")] == ["retired.prom"]


def test_a_failed_build_is_retried_after_a_back_off_and_the_process_recovers(
    telemetry: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []
    real_build = _sdk.build_metrics

    def flaky(settings: TelemetrySettings) -> _sdk.Metrics:
        attempts.append(1)
        if len(attempts) == 1:
            msg = "endpoint not configured yet"
            raise OSError(msg)
        return real_build(settings)

    telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "otlp")
    telemetry.setattr(_sdk, "build_metrics", flaky)
    telemetry.setattr(facade, "_RETRY_FIRST_S", 0.5)
    METRICS.close()  # starts the back-off over, at the shortened first step

    assert METRICS.get() is None
    assert METRICS.get() is None  # still backing off: no second attempt yet
    assert len(attempts) == 1
    deadline = time.monotonic() + 5
    while METRICS.get() is None:
        assert time.monotonic() < deadline, "never recovered"
        time.sleep(0.01)
    assert len(attempts) == 2
