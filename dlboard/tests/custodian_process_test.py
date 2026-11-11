"""
The custodian in a real `dlboard serve`: started by the server, replaced when it dies or hangs, untouched by a worker's
death, stopped with the server, and gone, with the web workers, when the server is killed outright.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pendulum
import psutil
import pytest
import requests

from dlboard import models
from dlboard.plugins.backend.artifact_purge_worker import PurgeTask
from dlboard.plugins.data_stores.sqlite import SQLLiteStore
from dlboard.serve._backend._purge_work import entity_purge

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="reads the process tree as Linux has it"
)

_DEADLINE_S = 20.0
_SHUTDOWN_DEADLINE_S = 90.0
"""
How long a server may take to exit after SIGTERM: its own budgets for stopping the custodian and the workers are 30s each
(`workers_kill_timeout_s`), so a shorter wait fails a shutdown the server is allowed to take, on a loaded machine (#605).
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        _, port = cast("tuple[str, int]", s.getsockname())
        return port


def _until[T](what: str, check: Callable[[], T | None], timeout: float = _DEADLINE_S) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (found := check()) is not None:
            return found
        time.sleep(0.2)
    pytest.fail(f"timed out waiting for {what}")


class _Server:
    def __init__(self, tmp_path: Path) -> None:
        self.port = _free_port()
        self.db = tmp_path / "db.sqlite"
        env = {
            **os.environ,
            "DLBOARD_SQLITE_LOCATION": str(self.db),
            "DLBOARD_ARTIFACT_STORE_LOCATION": str(tmp_path / "artifacts"),
            "DLBOARD_CUSTODIAN_TICK_S": "0.2",
            "DLBOARD_CUSTODIAN_POLL_S": "0.5",
            "DLBOARD_CUSTODIAN_BEAT_S": "0.5",
            "DLBOARD_CUSTODIAN_STALL_S": "3",
        }
        env.pop("DLBOARD_CUSTODIAN_ENABLED", None)
        cli = Path(sys.executable).with_name("dlboard")
        self.log_path = tmp_path / "server.log"
        self.log = self.log_path.open("w")
        self.master = subprocess.Popen(
            [
                str(cli),
                *("serve", "local", "--sqlite-location", str(self.db)),
                *("--artifact-store-location", str(tmp_path / "artifacts")),
                *("--port", str(self.port), "--workers", "2"),
            ],
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        try:
            _until("the server to be ready", self._ready)
        except BaseException:
            self.master.kill()
            self.master.wait()
            raise
        self.store = SQLLiteStore(self.db)

    def _ready(self) -> bool | None:
        try:
            return requests.get(f"http://127.0.0.1:{self.port}/readyz", timeout=1).ok or None
        # A connection refused, or a slow answer on a loaded machine: poll again.
        except requests.RequestException:
            return None

    def custodian(self, *, not_pid: int | None = None) -> int:
        """The pid of the custodian beating now (other than `not_pid`): a child of the server, alive."""

        def beating() -> int | None:
            children = {p.pid for p in psutil.Process(self.master.pid).children()}
            recent = self.store.custodians(seen_since=pendulum.now("UTC").subtract(seconds=2))
            pids = [c.pid for c in recent if c.pid != not_pid and c.pid in children]
            return pids[0] if pids else None

        return _until("a custodian to beat", beating)

    def descendants(self) -> list[psutil.Process]:
        return psutil.Process(self.master.pid).children(recursive=True)

    def stop(self) -> None:
        """
        Kill the server and everything it started, so that a failed test leaves nothing running.

        Found by the database each was started on, not as the master's children: a test may already have killed the
        master, and what outlived it is nobody's child any more.
        """
        if self.master.poll() is None:
            self.master.kill()
            self.master.wait()
        started = [p for p in psutil.process_iter(["cmdline"]) if str(self.db) in (p.info["cmdline"] or [])]
        for process in started:
            with contextlib.suppress(psutil.NoSuchProcess):  # gone with the server, as it should be
                process.kill()
        psutil.wait_procs(started, timeout=_DEADLINE_S)
        self.store.dispose()
        self.log.close()

    def output(self) -> str:
        """What the server (and its custodian) logged, so that a failure says what they were doing."""
        return self.log_path.read_text()


@pytest.fixture
def server(tmp_path: Path) -> Iterator[_Server]:
    server = _Server(tmp_path)
    yield server
    server.stop()


def test_the_server_keeps_one_custodian_alive_and_stops_it_with_itself(server: _Server) -> None:
    first = server.custodian()
    assert first in {p.pid for p in psutil.Process(server.master.pid).children()}

    os.kill(first, signal.SIGKILL)
    second = server.custodian(not_pid=first)

    os.kill(second, signal.SIGSTOP)  # hung: its heartbeat goes stale
    third = server.custodian(not_pid=second)
    assert not psutil.pid_exists(second) or psutil.Process(second).status() == psutil.STATUS_ZOMBIE

    workers = [p for p in psutil.Process(server.master.pid).children() if p.pid != third]
    assert workers
    workers[0].kill()
    time.sleep(2)
    assert server.custodian() == third

    # A purge queued as the admin page queues one is done by the custodian, not by any web process.
    admin = server.store.get_or_create_user(models.Principal.unverified("admin"))
    project = server.store.create_project(models.NewProject(name="purged", description=""))
    server.store.delete_project(project.id, admin)
    server.store.enqueue_work([entity_purge(models.EntityType.PROJECT, project.id, requested_by=admin.id)])
    server.store.request_task_run(PurgeTask.ENTITIES)
    _until(
        "the custodian to purge it",
        lambda: True if not list(server.store.list_deleted_projects()) else None,
    )

    everything = server.descendants()
    server.master.send_signal(signal.SIGTERM)
    server.master.wait(timeout=_SHUTDOWN_DEADLINE_S)
    _, alive = psutil.wait_procs(everything, timeout=_SHUTDOWN_DEADLINE_S)
    assert alive == []


def test_nothing_the_server_started_outlives_it_killed_outright(server: _Server) -> None:
    """Its custodian and its web workers too: an orphaned worker keeps serving the port and holding the database (#584)."""
    server.custodian()
    everything = server.descendants()
    server.master.kill()
    server.master.wait()
    _, alive = psutil.wait_procs(everything, timeout=_DEADLINE_S)
    assert alive == [], server.output()
