"""The server's watch on its custodian: a custodian still starting (migrating) gets longer than one that stopped beating."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast, override

import pytest

from dlboard.serve._custodian._process import CustodianProcess
from dlboard.serve._custodian._supervisor import CustodianSettings

if TYPE_CHECKING:
    from multiprocessing.process import BaseProcess
    from multiprocessing.sharedctypes import Synchronized


@dataclass
class _Process:
    """A custodian that is alive until it is ended."""

    alive: bool = True
    ended: list[str] = field(default_factory=list)
    pid: int = 1
    exitcode: int | None = None

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.ended.append("terminate")
        self.alive = False

    def kill(self) -> None:
        self.ended.append("kill")
        self.alive = False

    def join(self, timeout: float | None = None) -> None:
        """Nothing to wait for."""


@dataclass
class _Beat:
    value: float


class _Watched(CustodianProcess):
    """A `CustodianProcess` that starts no process, and watches one it was handed until it first restarts it."""

    @override
    def _restart(self, why: str) -> None:
        self._stopping.set()  # `why` is not needed: whether it was ended is what the test asserts


@pytest.mark.parametrize(
    ("beat", "started_ago", "ended"),
    [
        (
            0.0,
            10,
            False,
        ),  # still starting, and for longer than a beat may be quiet: a migration takes its time
        (0.0, 100, True),  # starting for longer than anything should take
        (-1.0, 100, True),  # beating once, and quiet since
        (1e9, 100, False),  # beating
    ],
    ids=["slow start", "stuck start", "stalled", "beating"],
)
def test_a_custodian_is_killed_when_its_heartbeat_is_stale_for_the_phase_it_is_in(
    beat: float, started_ago: float, *, ended: bool
) -> None:
    process = _Process()
    custodian = _Watched(CustodianSettings(tick_s=0.01, stall_s=1, startup_s=50))
    custodian._process = cast("BaseProcess", process)
    custodian._heartbeat = cast("Synchronized[float]", _Beat(beat))
    custodian._started = time.monotonic() - started_ago
    threading.Timer(
        0.2, custodian._stopping.set
    ).start()  # nothing ends the watch but a restart: give it ticks
    custodian._watch()
    assert bool(process.ended) is ended
