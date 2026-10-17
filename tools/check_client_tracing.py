"""
Prove the client's tracing works with whichever OpenTelemetry is installed -- run by CI against the oldest one.

`client/pyproject.toml`'s `otel` extra promises a floor (and no ceiling), so a user's own OpenTelemetry is never
forced to move for dlboard. That promise is only as good as a test that installs exactly the floor, and the newest
release, and an old protobuf, and runs the calls the client really makes: start spans, carry the context onward,
export to a collector. This does that against a throwaway one, using nothing but the installed client wheel.

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

_TRACED = """
import sys
import json
from dlboard._telemetry.traces import SpanKind, carrier, flush, name_service, span, trace_ids

name_service("dlboard-client")
with span("ship", SpanKind.INTERNAL), span("POST /api/v1/metrics", SpanKind.CLIENT):
    headers = carrier()
    ids = trace_ids()
with span("write", SpanKind.CONSUMER, parent=headers):
    pass
flush()
sys.stdout.write(json.dumps({"headers": headers, "ids": ids}) + "\\n")
"""

received: list[tuple[str, bytes]] = []


class _Collector(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        received.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        """Say nothing."""


def main() -> int:
    """Return a failing exit code, with what went wrong on stderr, if the client's tracing does not work here."""
    server = HTTPServer(("127.0.0.1", 0), _Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {
        "DLBOARD_TELEMETRY_TRACES": "true",
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{server.server_port}",
    }
    traced = subprocess.run(
        [sys.executable, "-c", _TRACED], env=env, capture_output=True, text=True, timeout=120, check=False
    )
    if traced.returncode != 0:
        sys.stderr.write(f"the traced process failed:\n{traced.stderr}\n")
        return 1

    result = json.loads(traced.stdout.splitlines()[-1])
    headers: dict[str, str] = result["headers"]
    ids: list[str] | None = result["ids"]
    problems = []
    if ids is None or set(headers) != {"traceparent"}:
        problems.append(f"no trace context to carry: {headers!r} {ids!r}")
    elif ids[0] not in headers["traceparent"]:
        problems.append(f"the carrier does not name the open trace: {headers!r} {ids!r}")
    bodies = [body for path, body in received if path == "/v1/traces"]
    if not bodies:
        problems.append(f"the collector received nothing on /v1/traces: {[path for path, _ in received]}")
    elif ids is not None and bytes.fromhex(ids[0]) not in b"".join(bodies):
        problems.append("the collector received spans, but not this trace's")
    for problem in problems:
        sys.stderr.write(problem + "\n")
    sys.stdout.write("ok\n" if not problems else "FAILED\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
