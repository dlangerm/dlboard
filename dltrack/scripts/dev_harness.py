"""
A local dev harness: start/seed/stop an isolated dltrack server for manual UI/API verification.

Every server this launches gets its own scratch directory with its own sqlite db and artifact
store, so it never touches a real deployment's data. Run via
`uv run python dltrack/scripts/dev_harness.py <command> --help`, never raw `python`.

Typical flow:
    uv run python dltrack/scripts/dev_harness.py start
    uv run python dltrack/scripts/dev_harness.py seed --dir /tmp/dltrack-harness-xyz
    # ... poke at it with curl / a browser / chromium-cli, pointed at the printed URL ...
    uv run python dltrack/scripts/dev_harness.py stop --dir /tmp/dltrack-harness-xyz
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Annotated

import cyclopts
import pendulum
import requests

from dltrack import models
from dltrack.plugins.backend.basic_rest_backend import BasicDltrackAPI

app = cyclopts.App(name="dev-harness", help="Start/seed/stop an isolated dltrack dev server.")

_STATE_FILENAME = "harness.json"
_DEFAULT_HOST = "127.0.0.1"
_RUNS_WITH_HPARAMS = 2


@dataclass(frozen=True)
class HarnessState:
    """Everything `seed`/`stop` need to find a server `start` already launched."""

    pid: int
    host: str
    port: int
    directory: str

    @property
    def base_url(self) -> str:
        """The server's root URL."""
        return f"http://{self.host}:{self.port}"


def _state_path(directory: Path) -> Path:
    return directory / _STATE_FILENAME


def _load_state(directory: Path) -> HarnessState:
    return HarnessState(**json.loads(_state_path(directory).read_text()))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_DEFAULT_HOST, 0))
        return sock.getsockname()[1]


def _wait_until_serving(base_url: str, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(requests.RequestException):
            if requests.get(base_url, timeout=1).status_code < HTTPStatus.INTERNAL_SERVER_ERROR:
                return
        time.sleep(0.3)
    msg = f"Server at {base_url} did not come up within {timeout}s"
    raise TimeoutError(msg)


@app.command
def start(
    *,
    directory: Annotated[
        Path | None,
        cyclopts.Parameter(
            name="--dir",
            help="Scratch directory for this server's db/artifacts/state. Default: a fresh tempdir.",
        ),
    ] = None,
    port: Annotated[int, cyclopts.Parameter(help="Port to bind. 0 (default) picks a free one.")] = 0,
    host: Annotated[str, cyclopts.Parameter(help="Interface to bind to.")] = _DEFAULT_HOST,
) -> None:
    """Launch an isolated dltrack dev server in the background and wait until it's serving."""
    directory = directory or Path(tempfile.mkdtemp(prefix="dltrack-harness-"))
    directory.mkdir(parents=True, exist_ok=True)
    if _state_path(directory).exists():
        existing = _load_state(directory)
        print(f"Already running at {existing.base_url} (pid {existing.pid}) -- stop it first.")
        raise SystemExit(1)

    resolved_port = port or _free_port()
    log_file = (directory / "server.log").open("w")
    process = subprocess.Popen(
        [
            "dltrack",
            "serve",
            "local",
            "--debug",
            "--sqlite-location",
            str(directory / "dev.sqlite"),
            "--artifact-store-location",
            str(directory / "artifacts"),
            "--host",
            host,
            "--port",
            str(resolved_port),
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    state = HarnessState(pid=process.pid, host=host, port=resolved_port, directory=str(directory))
    _wait_until_serving(state.base_url)
    _state_path(directory).write_text(json.dumps(asdict(state), indent=2))
    print(f"Serving at {state.base_url}")
    print(f"Scratch dir: {directory}")
    print(f"Seed with:   uv run python dltrack/scripts/dev_harness.py seed --dir {directory}")
    print(f"Stop with:   uv run python dltrack/scripts/dev_harness.py stop --dir {directory}")


@app.command
def stop(*, directory: Annotated[Path, cyclopts.Parameter(name="--dir")]) -> None:
    """Kill a server `start` launched (and the debug reloader's child) and remove its scratch dir."""
    state = _load_state(directory)
    # `start` launches with `start_new_session=True`, making `state.pid` its own process-group
    # leader -- signal the whole group so the `--debug` reloader's respawned child (a separate pid
    # `start` never captured) dies too, not just the launcher.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(state.pid, signal.SIGTERM)
    shutil.rmtree(directory, ignore_errors=True)
    print(f"Stopped process group {state.pid}, removed {directory}")


@dataclass(frozen=True)
class SeedResult:
    """IDs `seed` created/reused, so a caller can build URLs or chain further API calls."""

    project_id: int
    experiment_id: int
    run_ids: list[int]


@app.command
def seed(  # noqa: PLR0913
    *,
    directory: Annotated[
        Path | None, cyclopts.Parameter(name="--dir", help="A directory `start` printed.")
    ] = None,
    base_url: Annotated[str | None, cyclopts.Parameter(help="Server URL, if not using --dir.")] = None,
    project: Annotated[str, cyclopts.Parameter(help="Project name (get-or-create).")] = "harness",
    experiment: Annotated[str, cyclopts.Parameter(help="Experiment name (get-or-create).")] = "exp1",
    runs: Annotated[int, cyclopts.Parameter(help="Number of runs to create.")] = 3,
    with_hparams: Annotated[
        bool, cyclopts.Parameter(help="Log a couple of hyperparameters on the first two runs.")
    ] = True,
    with_metrics: Annotated[bool, cyclopts.Parameter(help="Log metric steps on every run.")] = True,
    metric_steps: Annotated[
        int,
        cyclopts.Parameter(
            help="Steps to log per run, e.g. a large value to reproduce volume-sensitive perf issues."
        ),
    ] = 5,
) -> None:
    """Seed a running dltrack server with a project/experiment/runs via the real client API."""
    if directory is not None:
        resolved_base_url = _load_state(directory).base_url
    elif base_url is not None:
        resolved_base_url = base_url
    else:
        msg = "Pass --dir (from `start`) or --base-url."
        raise SystemExit(msg)

    api = BasicDltrackAPI(base_url=resolved_base_url)
    proj = api.get_or_create_project(project)
    exp = api.get_or_create_experiment(proj.id, name=experiment)

    run_ids: list[int] = []
    for i in range(runs):
        run = api.create_run(models.NewRun(experiment_id=exp.id, name=f"run-{i + 1}"))
        run_ids.append(run.id)
        if with_hparams and i < _RUNS_WITH_HPARAMS:
            api.log_hyperparams(
                models.NewHyperParams.from_raw(
                    run.id, exp.id, {"lr": 0.01 * (i + 1), "batch_size": 32 * (i + 1)}
                )
            )
        if with_metrics:
            api.log_metric_batch(
                [
                    models.LoggedMetrics(
                        metrics={"loss": 1.0 / (step + 1)},
                        step=step,
                        experiment_id=exp.id,
                        run_id=run.id,
                        timestamp_utc=pendulum.now(pendulum.UTC),
                    )
                    for step in range(metric_steps)
                ]
            )

    print(json.dumps(asdict(SeedResult(project_id=proj.id, experiment_id=exp.id, run_ids=run_ids)), indent=2))
    print(f"{resolved_base_url}/experiment/{exp.id}")


def main() -> None:
    """Entrypoint -- run via `uv run python dltrack/scripts/dev_harness.py`."""
    app()


if __name__ == "__main__":
    main()
