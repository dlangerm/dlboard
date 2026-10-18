"""Tests for `dlboard.serve._startup`: waiting out a dependency that is still coming up."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from dlboard._telemetry.traces import flush
from dlboard.serve import Dependency, _startup, wait_for

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@dataclass
class _Clock:
    """Stands in for `time` in `_startup`: sleeping moves the clock, and takes no time."""

    now: float = 0
    slept: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(_startup, "time", fake)
    return fake


class _Flaky:
    """An attempt that fails `failures` times with `error`, then works."""

    def __init__(self, failures: int, error: Exception) -> None:
        self.failures = failures
        self.error = error
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return "up"


def _is_oserror(exc: Exception) -> bool:
    return isinstance(exc, OSError)


def test_a_transient_failure_is_retried_with_a_growing_delay_until_it_clears(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLBOARD_STARTUP_WAIT_S", "100")
    attempt = _Flaky(6, OSError("no route"))

    assert wait_for(Dependency.DATABASE, attempt, transient=_is_oserror) == "up"

    assert attempt.calls == 7
    assert clock.slept == [1, 2, 4, 8, 10, 10]


@pytest.mark.parametrize(("wait_s", "calls", "slept"), [("0", 1, []), ("5", 4, [1, 2, 2])])
def test_a_transient_failure_is_raised_once_the_wait_is_over(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch, wait_s: str, calls: int, slept: list[float]
) -> None:
    monkeypatch.setenv("DLBOARD_STARTUP_WAIT_S", wait_s)
    attempt = _Flaky(1_000, OSError("no route"))

    with pytest.raises(OSError, match="no route"):
        wait_for(Dependency.DATABASE, attempt, transient=_is_oserror)

    assert attempt.calls == calls
    assert clock.slept == slept


def test_an_error_waiting_cannot_fix_is_raised_at_once(clock: _Clock) -> None:
    attempt = _Flaky(1, ValueError("wrong password"))

    with pytest.raises(ValueError, match="wrong password"):
        wait_for(Dependency.DATABASE, attempt, transient=_is_oserror)

    assert attempt.calls == 1
    assert clock.slept == []


def test_each_retry_is_an_event_on_the_wait_span(clock: _Clock, span_exporter: InMemorySpanExporter) -> None:
    wait_for(Dependency.OBJECT_STORE, _Flaky(2, OSError("refused")), transient=_is_oserror)
    flush()

    (wait,) = span_exporter.get_finished_spans()
    assert wait.name == "dlboard.startup.wait"
    assert dict(wait.attributes or {}) == {"dlboard.startup.dependency": "object store"}
    assert [(e.name, dict(e.attributes or {})["dlboard.startup.attempt"]) for e in wait.events] == [
        ("retry", 1),
        ("retry", 2),
    ]
    assert clock.slept == [1, 2]
