"""
The custodian process: started, watched and stopped by the server (Granian's master), never by a web worker.

`CustodianProcess` lives in the master. It starts the custodian before the workers and stops it after them, reaps and
restarts it when it exits (backing off when it keeps dying), and kills and restarts it when its heartbeat (shared memory,
so that this works with the database down) goes stale: after `DLBOARD_CUSTODIAN_STARTUP_S` if it never began (building the app
runs the migrations, which may take a while), `DLBOARD_CUSTODIAN_STALL_S` once it has. The custodian dies with the master: the kernel tells it on Linux
(`PR_SET_PDEATHSIG`), and it checks its parent every tick everywhere else, so no custodian outlives its server.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import signal
import sys
import threading
import time
from typing import TYPE_CHECKING, Final

from structlog.stdlib import get_logger

from dlboard._mp_context import SPAWN_CONTEXT
from dlboard.serve._custodian._supervisor import CustodianSettings, ExitCode, Supervisor, doubling

if TYPE_CHECKING:
    from multiprocessing.process import BaseProcess
    from multiprocessing.sharedctypes import Synchronized

_log = get_logger(__name__)

_PR_SET_PDEATHSIG: Final = 1
_BACKOFF: Final = (1.0, 60.0)
"""How long the master waits before starting a custodian that died again: doubling, from the first to the second."""
_HEALTHY_AFTER_S: Final = 60.0
"""A custodian that ran this long before it died was not crash-looping: the next one starts at once."""
_CRASH_LOOP: Final = 3
"""How many fast deaths in a row the log calls a crash loop."""
_STARTING: Final = 0.0
"""A custodian's heartbeat until its supervisor first beats: it is still starting."""
_STOP_TIMEOUT_S: Final = 30.0
"""How long `stop` gives a custodian to finish its batch and exit, when the caller says nothing."""


def die_with_parent() -> None:
    """Have the kernel send this process SIGTERM when its parent dies (Linux); elsewhere the caller checks its parent."""
    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)


def custodian_main(heartbeat: Synchronized[float], parent_pid: int) -> None:
    """
    The custodian process: build the app as a worker does, then run its plugins' tasks until told to stop.

    It reads the same environment a worker does (`WSGISettings`: the plugin list), so its stores are the server's.
    """
    die_with_parent()
    # Deferred: this module is imported by the master, which must not build the app (Granian's workers do).
    from dlboard.serve import app as build_app
    from dlboard.serve._custodian._role import become_custodian
    from dlboard.serve._custodian._store import get_system_task_store
    from dlboard.serve._custodian._task import registered_tasks
    from dlboard.serve._logging import configure_logging
    from dlboard.serve._production_server import WSGISettings

    configure_logging()
    become_custodian()
    settings = WSGISettings()
    app = build_app(settings.dlboard_plugins, url_prefix=settings.dlboard_url_prefix)

    def beat() -> None:
        heartbeat.value = time.monotonic()

    supervisor = Supervisor(
        app,
        registered_tasks(app),
        get_system_task_store(app),
        heartbeat=beat,
        parent_alive=lambda: os.getppid() == parent_pid,
    )
    signal.signal(signal.SIGTERM, lambda *_: supervisor.stop.set())
    signal.signal(signal.SIGINT, lambda *_: supervisor.stop.set())
    raise SystemExit(supervisor.run())


class CustodianProcess:
    """The server's handle on its one custodian: start it, keep it alive, and stop it."""

    def __init__(
        self, settings: CustodianSettings | None = None, *, stop_timeout_s: float = _STOP_TIMEOUT_S
    ) -> None:
        self._settings = settings or CustodianSettings()
        self._stop_timeout_s = stop_timeout_s
        self._process: BaseProcess | None = None
        self._heartbeat: Synchronized[float] | None = None
        self._started = 0.0
        self._failures = 0
        self._stopping = threading.Event()
        self._watcher: threading.Thread | None = None

    @property
    def pid(self) -> int | None:
        """The running custodian's pid, if one is running."""
        return self._process.pid if self._process is not None and self._process.is_alive() else None

    def start(self) -> None:
        """Start the custodian, and the thread that keeps it running. A Granian `on_startup` hook: before the workers."""
        if not self._settings.enabled:
            _log.warning("DLBOARD_CUSTODIAN_ENABLED is off: no background task runs on this server")
            return
        self._spawn()
        # It holds no lock while it sleeps, so a worker Granian forks meanwhile inherits nothing it could deadlock on.
        self._watcher = threading.Thread(target=self._watch, daemon=True, name="custodian-watcher")
        self._watcher.start()

    def stop(self) -> None:
        """Stop the custodian: SIGTERM, then SIGKILL if it outlasts the timeout. A Granian `on_shutdown` hook."""
        self._stopping.set()
        if self._watcher is not None:
            self._watcher.join(timeout=self._settings.tick_s * 3)
        self._end(timeout=self._stop_timeout_s)

    def _spawn(self) -> None:
        heartbeat = SPAWN_CONTEXT.Value("d", _STARTING)
        process = SPAWN_CONTEXT.Process(
            target=custodian_main, args=(heartbeat, os.getpid()), name="dlboard-custodian", daemon=False
        )
        process.start()
        self._process, self._heartbeat, self._started = process, heartbeat, time.monotonic()
        _log.info("Started the custodian", pid=process.pid)

    def _end(self, *, timeout: float) -> None:
        """Stop the custodian if it runs, and reap it either way, so that it never lingers as a zombie."""
        process = self._process
        if process is None:
            return
        if process.is_alive():
            process.terminate()
            process.join(timeout)
        if process.is_alive():
            _log.warning("The custodian did not stop in time; killing it", pid=process.pid)
            process.kill()
        process.join()

    def _watch(self) -> None:
        while not self._stopping.wait(self._settings.tick_s):
            process, heartbeat = self._process, self._heartbeat
            if process is None or heartbeat is None:
                return
            if not process.is_alive():
                process.join()
                self._restart(f"it exited ({_why(process.exitcode)})")
                continue
            starting = heartbeat.value == _STARTING
            quiet_for = time.monotonic() - (self._started if starting else heartbeat.value)
            if quiet_for > (self._settings.startup_s if starting else self._settings.stall_s):
                _log.error(
                    "The custodian's heartbeat went stale; killing it", pid=process.pid, starting=starting
                )
                self._end(timeout=0)
                self._restart("its heartbeat went stale")

    def _restart(self, why: str) -> None:
        crashed_fast = time.monotonic() - self._started < _HEALTHY_AFTER_S
        self._failures = self._failures + 1 if crashed_fast else 0
        wait = doubling(*_BACKOFF, self._failures - 1) if self._failures else 0.0
        _log.error(
            "Restarting the custodian", because=why, after_s=wait, crash_loop=self._failures > _CRASH_LOOP
        )
        if not self._stopping.wait(wait):
            self._spawn()


def _why(exitcode: int | None) -> str:
    match exitcode:
        case None:
            return "still running"
        case code if code < 0:
            return f"killed by signal {-code}"
        case code:
            try:
                return ExitCode(code).name.lower()
            except ValueError:
                return f"exit code {code}"
