"""
The server's watch on its custodian: a custodian still starting (migrating) gets longer than one that stopped beating, and
a beat never waits on the server.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast, override

import pytest

from dlboard._mp_context import SPAWN_CONTEXT
from dlboard.serve._custodian._process import CustodianProcess, new_heartbeat
from dlboard.serve._custodian._supervisor import CustodianSettings

if TYPE_CHECKING:
    import ctypes
    from multiprocessing.process import BaseProcess
    from multiprocessing.synchronize import Event


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
    custodian._heartbeat = cast("ctypes.c_double", _Beat(beat))
    custodian._started = time.monotonic() - started_ago
    threading.Timer(
        0.2, custodian._stopping.set
    ).start()  # nothing ends the watch but a restart: give it ticks
    custodian._watch()
    assert bool(process.ended) is ended


def _read_forever(heartbeat: ctypes.c_double, ready: Event) -> None:
    ready.set()
    while True:
        _ = heartbeat.value


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="freezes a process with SIGSTOP")
def test_a_custodian_beats_while_its_server_is_frozen_reading_the_beat() -> None:
    """A server stopped, or killed, in the middle of reading the beat holds nothing the custodian's next beat waits on (#788)."""
    heartbeat, ready = new_heartbeat(), SPAWN_CONTEXT.Event()
    server = SPAWN_CONTEXT.Process(target=_read_forever, args=(heartbeat, ready), daemon=True)
    server.start()
    assert server.pid is not None
    try:
        assert ready.wait(timeout=60)
        for at in range(50):  # frozen wherever it happens to be: somewhere in its read, as often as not
            time.sleep(0.005)
            os.kill(server.pid, signal.SIGSTOP)
            beat = threading.Thread(target=setattr, args=(heartbeat, "value", float(at)), daemon=True)
            beat.start()
            beat.join(timeout=2)
            assert not beat.is_alive(), f"beat {at} waited on the frozen server"
            os.kill(server.pid, signal.SIGCONT)
    finally:
        server.kill()
        server.join()
