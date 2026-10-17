"""
Prove the client's tracing works with whichever OpenTelemetry is installed -- run by CI against the oldest one.

`client/pyproject.toml`'s `otel` extra promises a floor (and no ceiling), so a user's own OpenTelemetry is never
forced to move for dlboard. That promise is only as good as a test that installs exactly the floor, and the newest
release, and an old protobuf, and runs the calls the client really makes: start spans, carry the context onward
(through a real `BasicDlboardAPI` request to a stub server, and through a queue-style carrier), export to a
collector. This does that against a throwaway one, using nothing but the installed client wheel.

The traced code runs in a process of its own, as the client's shipping processes do, with exactly the environment
a user would set.

    uv pip install --python venv/bin/python --resolution lowest-direct -r client/pyproject.toml --extra otel
    uv pip install --python venv/bin/python --no-deps dlboard_client-X.whl
    venv/bin/python tools/check_client_tracing.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from dlboard._wire import API_VERSION, REQUEST_ID_HEADER, WHOAMI_PATH

_TRACED = """
import sys
import json
from dlboard._telemetry.traces import SpanKind, carrier, flush, name_service, span, trace_ids
from dlboard.client._rest_api import BasicDlboardAPI

name_service("dlboard-client")
with span("ship", SpanKind.INTERNAL), span("POST /api/v1/metrics", SpanKind.CLIENT):
    headers = carrier()
    ids = trace_ids()
with span("write", SpanKind.CONSUMER, parent=headers):
    pass
with span("ship", SpanKind.INTERNAL):
    BasicDlboardAPI(base_url=sys.argv[1]).whoami()
    request_ids = trace_ids()
flush()
sys.stdout.write(json.dumps({"headers": headers, "ids": ids, "request_ids": request_ids}) + "\\n")
"""

collected: list[tuple[str, bytes]] = []
whoami_headers: list[dict[str, str]] = []


class _Stub(BaseHTTPRequestHandler):
    """Both a collector (`POST /v1/traces`) and a dlboard server that only knows `whoami`."""

    def do_POST(self) -> None:
        collected.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()

    def do_GET(self) -> None:
        whoami_headers.append(dict(self.headers))
        body = json.dumps(
            {"id": 1, "username": "u", "server_version": "0", "api_version": API_VERSION}
        ).encode()
        self.send_response(200 if self.path == f"/{WHOAMI_PATH}" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        """Say nothing."""


def main() -> int:
    """Return a failing exit code, with what went wrong on stderr, if the client's tracing does not work here."""
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    env = {"DLBOARD_TELEMETRY_TRACES": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": url}
    traced = subprocess.run(
        [sys.executable, "-c", _TRACED, url],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if traced.returncode != 0:
        sys.stderr.write(f"the traced process failed:\n{traced.stderr}\n")
        return 1

    result = json.loads(traced.stdout.splitlines()[-1])
    headers: dict[str, str] = result["headers"]
    ids: list[str] | None = result["ids"]
    request_ids: list[str] | None = result["request_ids"]
    problems = []
    if ids is None or set(headers) != {"traceparent"}:
        problems.append(f"no trace context to carry: {headers!r} {ids!r}")
    elif ids[0] not in headers["traceparent"]:
        problems.append(f"the carrier does not name the open trace: {headers!r} {ids!r}")
    sent = {key.lower(): value for key, value in (whoami_headers[-1] if whoami_headers else {}).items()}
    if request_ids is None or request_ids[0] not in sent.get("traceparent", ""):
        problems.append(f"the request did not carry its trace to the server: {sent!r} {request_ids!r}")
    if not sent.get(REQUEST_ID_HEADER.lower()):
        problems.append(f"the request carried no request id: {sent!r}")
    bodies = [body for path, body in collected if path == "/v1/traces"]
    if not bodies:
        problems.append(f"the collector received nothing on /v1/traces: {[path for path, _ in collected]}")
    elif ids is not None and bytes.fromhex(ids[0]) not in b"".join(bodies):
        problems.append("the collector received spans, but not this trace's")
    for problem in problems:
        sys.stderr.write(problem + "\n")
    sys.stdout.write("ok\n" if not problems else "FAILED\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
