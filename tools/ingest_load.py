"""
Upload artifacts to a dlboard server under load, and report where the blob ingest's time goes (#392).

With no `--server-url`, it brings up a stack of its own: Postgres in a throwaway container, `dlboard serve custom` over
it with tracing and Prometheus metrics on, and a collector in this process that keeps the server's spans. A client
then uploads images (and a metric per step) for `--duration-s`, while `--page-load` headless browsers keep the run's
experiment page open, polling through live updates. At the end it reports each `dlboard.blob.ingest` span's duration
against what its child spans account for, and the server's own artifact histograms.

Run it with and without page load: if ingest slows only while pages are open, its thread is waiting on the request
threads (the GIL), not on the database. `--profile` runs the server under py-spy too (`wall` samples idle threads as
well, `gil` only the one holding the GIL), for a speedscope profile of where each thread was.

    uv run tools/ingest_load.py --page-load 0
    uv run tools/ingest_load.py --page-load 8 --profile wall
    uv run tools/ingest_load.py --server-url https://dlboard.example.com --page-load 4  # load only: read its traces there

Needs Docker, and Playwright's Chromium for page load (`uv run playwright install chromium`).
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final

import cyclopts
import numpy as np
import pendulum
import requests
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from prometheus_client.parser import text_string_to_metric_families
from pydantic import SecretStr

from dlboard import models
from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.client.artifacts.image import Image
from dlboard.plugins import BUILTIN_BACKEND, BUILTIN_CHARTS, LOCAL_AUTH, POSTGRES_STORAGE, themes

if TYPE_CHECKING:
    from collections.abc import Iterator
    from wsgiref.types import StartResponse, WSGIEnvironment

    from opentelemetry.proto.trace.v1.trace_pb2 import Span as WireSpan

    from dlboard.models import PluginProtocol

PLUGINS: list[PluginProtocol] = [
    *POSTGRES_STORAGE,
    *LOCAL_AUTH,
    *BUILTIN_BACKEND,
    *BUILTIN_CHARTS,
    themes.default,
]
"""What the server this starts runs, as `--plugins ingest_load:PLUGINS`: a Postgres deployment with no sign-in."""

_HOST: Final = "127.0.0.1"
_INGEST_SPAN: Final = "dlboard.blob.ingest"
_EXPORT_SETTLE_S: Final = 3.0
"""How long after the load stops the server gets to record the last refs and export their spans."""
_HISTORY_STEPS_PER_UPLOAD: Final = 100


class Profile(StrEnum):
    """Whether to run the server under py-spy, and what it samples."""

    NONE = "none"
    WALL = "wall"
    """Every thread, idle ones included: a thread waiting for the GIL shows up idle inside code that never sleeps."""
    GIL = "gil"
    """Only the thread holding the GIL: who the waiting ones were waiting for."""


@dataclass
class _Collector:
    """An OTLP/HTTP collector that keeps every span sent to it."""

    spans: list[WireSpan] = field(default_factory=list)

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> list[bytes]:
        body = environ["wsgi.input"].read(int(environ["CONTENT_LENGTH"]))
        if environ["PATH_INFO"] == "/v1/traces":
            for resource_spans in ExportTraceServiceRequest.FromString(body).resource_spans:
                for scope_spans in resource_spans.scope_spans:
                    self.spans.extend(scope_spans.spans)
        start_response("200 OK", [("Content-Type", "application/x-protobuf")])
        return [b""]


@dataclass
class _Uploads:
    """What the client saw: how long each upload request took."""

    seconds: list[float] = field(default_factory=list)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_HOST, 0))
        return sock.getsockname()[1]


def _wait_until_ready(base_url: str, timeout_s: float = 120) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with contextlib.suppress(requests.RequestException):
            if requests.get(f"{base_url}/readyz", timeout=1).ok:
                return
        time.sleep(0.5)
    msg = f"{base_url} was not ready within {timeout_s}s"
    raise TimeoutError(msg)


@dataclass(frozen=True)
class _Server:
    """How the server this starts runs."""

    workers: int
    poll_ms: int
    profile: Profile


@contextlib.contextmanager
def _local_stack(out: Path, server_options: _Server, collector: _Collector) -> Iterator[str]:
    """Postgres, a collector and a traced server over them; yields the server's URL, and stops it on the way out."""
    from wsgiref.simple_server import WSGIRequestHandler, make_server

    from testcontainers.community.postgres import PostgresContainer

    class _Quiet(WSGIRequestHandler):
        def log_message(self, *_args: object) -> None: ...

    otlp = make_server(_HOST, 0, collector, handler_class=_Quiet)
    threading.Thread(target=otlp.serve_forever, daemon=True).start()
    with PostgresContainer(
        "postgres:17-alpine", username="dlboard", password="dlboard", dbname="dlboard"
    ) as pg:
        port = _free_port()
        # The server inherits this process's environment, plus what points it at the stack started here.
        env = os.environ | {  # dlboard-ignore[forbid-string-ignore]
            "PYTHONPATH": str(Path(__file__).parent),
            "DLBOARD_POSTGRES_HOST": pg.get_container_host_ip(),
            "DLBOARD_POSTGRES_PORT": str(pg.get_exposed_port(5432)),
            "DLBOARD_POSTGRES_DATABASE": "dlboard",
            "DLBOARD_POSTGRES_USER": "dlboard",
            "DLBOARD_POSTGRES_PASSWORD": "dlboard",
            "DLBOARD_POSTGRES_SSLMODE": "disable",
            "DLBOARD_ARTIFACT_STORE_LOCATION": str(out / "artifacts"),
            "DLBOARD_TELEMETRY_TRACES": "true",
            "DLBOARD_TELEMETRY_METRICS": "prometheus",
            "DLBOARD_TELEMETRY_PROMETHEUS_SNAPSHOT_S": "1",
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://{_HOST}:{otlp.server_port}",
            "OTEL_BSP_SCHEDULE_DELAY": "500",
            "DLBOARD_POLL_INTERVAL_MS": str(server_options.poll_ms),
        }
        serve = ["dlboard", "serve", "custom", "--plugins", "ingest_load:PLUGINS", "--port", str(port)]
        serve += ["--workers", str(server_options.workers)]
        match server_options.profile:
            case Profile.NONE:
                command = serve
            case Profile.WALL | Profile.GIL as profile:
                sampled = "--idle" if profile is Profile.WALL else "--gil"
                command = ["uvx", "py-spy", "record", "--subprocesses", "--threads", sampled, "--format"]
                command += ["speedscope", "-o", str(out / f"profile-{profile}.json"), "--", *serve]
        with (out / "server.log").open("w") as log:
            server = subprocess.Popen(
                command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
        try:
            base_url = f"http://{_HOST}:{port}"
            _wait_until_ready(base_url)
            yield base_url
        finally:
            # SIGINT, not SIGTERM: py-spy writes its profile only once the server it runs has stopped.
            os.killpg(server.pid, signal.SIGINT)
            server.wait(timeout=60)
            otlp.shutdown()


@dataclass(frozen=True)
class _Load:
    """What the client uploads, how often, and until when."""

    deadline: float
    every_s: float
    images: int
    side: int


def _upload(api: BasicDlboardAPI, run: models.Run, load: _Load, staging: Path, uploads: _Uploads) -> None:
    """Log `load.images` random images and a metric per step until its deadline, a step every `load.every_s`."""
    rng = np.random.default_rng(0)
    step = 0
    while time.monotonic() < load.deadline:
        started = time.monotonic()
        batch = [
            Image(
                key=f"images/{i}",
                image=rng.integers(0, 255, (load.side, load.side, 3), dtype=np.uint8),
                step=step,
            ).to_artifact(staging, run.id, run.experiment_id)
            for i in range(load.images)
        ]
        api.log_artifact_batch(batch)
        uploads.seconds.append(time.monotonic() - started)
        api.log_metric_batch([_step(run, step, ["loss"])])
        for _, path in batch:
            path.unlink()
        step += 1
        time.sleep(max(load.every_s - (time.monotonic() - started), 0))


def _step(run: models.Run, step: int, keys: list[str]) -> models.LoggedMetrics:
    return models.LoggedMetrics(
        metrics={key: (i + 1) / (step + 1) for i, key in enumerate(keys)},
        step=step,
        experiment_id=run.experiment_id,
        run_id=run.id,
        timestamp_utc=pendulum.now(pendulum.UTC),
    )


@dataclass(frozen=True)
class _History:
    """Metrics already logged when the load starts, so the experiment page has as much to read as a real one."""

    runs: int
    keys: int
    steps: int

    def seed(self, api: BasicDlboardAPI, experiment_id: int) -> None:
        keys = [f"history/metric_{i:04d}" for i in range(self.keys)]
        for i in range(self.runs):
            run = api.create_run(models.NewRun(experiment_id=experiment_id, name=f"history-{i}"))
            for first in range(0, self.steps, _HISTORY_STEPS_PER_UPLOAD):
                last = min(first + _HISTORY_STEPS_PER_UPLOAD, self.steps)
                api.log_metric_batch([_step(run, step, keys) for step in range(first, last)])


def _keep_pages_open(url: str, pages: int, deadline: float) -> None:
    """Hold `pages` headless browser tabs on `url` until `deadline`; the experiment page polls on its own."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for _ in range(pages):
            browser.new_page().goto(url)
        time.sleep(max(deadline - time.monotonic(), 0))
        browser.close()


def _seconds(nanos: int) -> float:
    return nanos / 1e9


def _quantiles(values: list[float]) -> str:
    if not values:
        return "none"
    ordered = sorted(values)
    p95 = ordered[min(int(len(ordered) * 0.95), len(ordered) - 1)]
    return f"n={len(values)} p50={statistics.median(ordered) * 1000:.0f}ms p95={p95 * 1000:.0f}ms max={ordered[-1] * 1000:.0f}ms"


def _report_ingest(spans: list[WireSpan]) -> str:
    """Each ingest span's duration against the share of it its children cover, and its thread's CPU time if recorded."""
    children: dict[bytes, list[WireSpan]] = defaultdict(list)
    for span in spans:
        children[span.parent_span_id].append(span)
    ingests = [span for span in spans if span.name == _INGEST_SPAN]
    durations = [_seconds(s.end_time_unix_nano - s.start_time_unix_nano) for s in ingests]
    covered = [
        sum(_seconds(c.end_time_unix_nano - c.start_time_unix_nano) for c in children[s.span_id])
        for s in ingests
    ]
    cpu = [a.value.double_value for s in ingests for a in s.attributes if a.key == "dlboard.thread.cpu_time"]
    lines = [
        f"{_INGEST_SPAN}:   {_quantiles(durations)}",
        f"  its children:        {_quantiles(covered)}",
        f"  untraced:            {_quantiles([d - c for d, c in zip(durations, covered, strict=True)])}",
    ]
    if cpu:
        lines.append(f"  thread CPU time:     {_quantiles(cpu)}")
    requests_by_name: dict[str, list[float]] = defaultdict(list)
    for span in spans:
        if span.kind == span.SPAN_KIND_SERVER:
            requests_by_name[span.name].append(_seconds(span.end_time_unix_nano - span.start_time_unix_nano))
    lines += [f"{name}: {_quantiles(durations)}" for name, durations in sorted(requests_by_name.items())]
    return "\n".join(lines)


def _report_histograms(base_url: str) -> str:
    """The mean of every `dlboard_artifact_*` histogram the server's `/metrics` adds up across its processes."""
    scrape = requests.get(f"{base_url}/metrics", timeout=10).text
    lines = []
    for family in text_string_to_metric_families(scrape):
        if family.type == "histogram" and family.name.startswith("dlboard_artifact"):
            total = sum(s.value for s in family.samples if s.name.endswith("_sum"))
            count = sum(s.value for s in family.samples if s.name.endswith("_count"))
            if count:
                lines.append(f"{family.name}: n={count:.0f} mean={total / count * 1000:.0f}ms")
    return "\n".join(lines)


app = cyclopts.App(help=__doc__)


@app.default
def main(  # noqa: PLR0913
    *,
    server_url: Annotated[
        str | None, cyclopts.Parameter(help="Drive this server instead of starting one.")
    ] = None,
    api_key: Annotated[str | None, cyclopts.Parameter(env_var="DLBOARD_API_KEY")] = None,
    duration_s: float = 60,
    upload_every_s: Annotated[
        float, cyclopts.Parameter(help="Seconds between one step's upload and the next.")
    ] = 0.5,
    images_per_upload: int = 4,
    image_side: Annotated[
        int, cyclopts.Parameter(help="Each random RGB image is this many pixels square.")
    ] = 256,
    page_load: Annotated[int, cyclopts.Parameter(help="Browser tabs holding the experiment page open.")] = 0,
    history_runs: Annotated[
        int, cyclopts.Parameter(help="Runs of metric history logged before the load.")
    ] = 0,
    history_keys: int = 50,
    history_steps: int = 2000,
    workers: Annotated[
        int, cyclopts.Parameter(help="Server worker processes, when this starts the server.")
    ] = 2,
    poll_ms: Annotated[
        int, cyclopts.Parameter(help="How often an open page polls, when this starts the server.")
    ] = 30000,
    profile: Profile = Profile.NONE,
    out: Annotated[
        Path | None, cyclopts.Parameter(help="Where the server log and profile go: a temp dir by default.")
    ] = None,
) -> None:
    """Upload under load, then report the ingest's spans and the server's artifact histograms."""
    out = out or Path(tempfile.mkdtemp(prefix="dlboard-ingest-load-"))
    out.mkdir(parents=True, exist_ok=True)
    collector = _Collector()
    uploads = _Uploads()
    with contextlib.ExitStack() as stack:
        base_url = server_url or stack.enter_context(
            _local_stack(out, _Server(workers, poll_ms, profile), collector)
        )
        api = BasicDlboardAPI(base_url, SecretStr(api_key) if api_key else None)
        project = api.get_or_create_project("ingest-load")
        experiment = api.get_or_create_experiment(project.id, name=f"load-{pendulum.now():%Y%m%d-%H%M%S}")
        _History(history_runs, history_keys, history_steps).seed(api, experiment.id)
        run = api.create_run(models.NewRun(experiment_id=experiment.id, name=f"pages-{page_load}"))
        load = _Load(time.monotonic() + duration_s, upload_every_s, images_per_upload, image_side)
        staging = Path(tempfile.mkdtemp(dir=out))
        uploader = threading.Thread(target=_upload, args=(api, run, load, staging, uploads))
        uploader.start()
        if page_load:
            _keep_pages_open(f"{base_url}/experiment/{experiment.id}", page_load, load.deadline)
        uploader.join()
        time.sleep(_EXPORT_SETTLE_S)
        report = [f"page load: {page_load} tab(s)", f"upload requests:     {_quantiles(uploads.seconds)}"]
        if server_url is None:
            report += [_report_histograms(base_url)]
    if server_url is None:
        report += [_report_ingest(collector.spans), f"server log and profile: {out}"]
    sys.stdout.write("\n".join(report) + "\n")


if __name__ == "__main__":
    app()
