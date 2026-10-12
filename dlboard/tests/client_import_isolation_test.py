"""
Neither side of dlboard needs the other's heavy dependencies just to be imported.

Run in a subprocess, not in-process: this test suite's own `conftest.py` imports `dash` (and
`torch`, transitively via `pytorch_lightning`) long before any test function runs, so by the time
a test body executes, checking `sys.modules` here would prove nothing. A real client-only install
(`pip install dlboard`, no `[server]` extra) doesn't have dash, flask, sqlalchemy, pandas, granian
or dash-mantine-components/dash-ag-grid installed at all; a real server-only install doesn't have
torch or pytorch_lightning. Each test simulates that by making every import of one of those fail,
in a fresh process that otherwise has every dependency this repo's dev env does.
"""

from __future__ import annotations

import subprocess
import sys

_SERVER_ONLY_PACKAGES = (
    "dash",
    "dash_mantine_components",
    "dash_ag_grid",
    "flask",
    "werkzeug",
    "sqlalchemy",
    "pandas",
    "granian",
)

_CLIENT_ONLY_PACKAGES = (
    "torch",
    "pytorch_lightning",
    "lightning_fabric",
)


def _run_with_blocked_imports(blocked: tuple[str, ...], script: str) -> subprocess.CompletedProcess[str]:
    guard = f"""
import builtins
_blocked = {blocked!r}
_real_import = builtins.__import__
def _guarded(name, *a, **kw):
    if name.split(".")[0] in _blocked:
        raise ImportError(f"simulated: {{name}} not installed")
    return _real_import(name, *a, **kw)
builtins.__import__ = _guarded
"""
    return subprocess.run([sys.executable, "-c", guard + script], capture_output=True, text=True, check=False)


def test_importing_the_client_never_needs_the_server_stack() -> None:
    script = """
from dlboard.client.dlboard_logger import DLBoardLogger, DLBoardLoggerSettings
from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.client.artifacts.image import Image
from dlboard.client.artifacts.link import Link
print("ok")
"""
    result = _run_with_blocked_imports(_SERVER_ONLY_PACKAGES, script)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_importing_the_server_never_needs_torch() -> None:
    script = """
import dlboard.plugins.backend.basic_rest_backend
import dlboard.serve.app
print("ok")
"""
    result = _run_with_blocked_imports(_CLIENT_ONLY_PACKAGES, script)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


_OPENTELEMETRY_PACKAGES = ("opentelemetry", "prometheus_client")


def test_a_server_with_telemetry_off_never_loads_the_opentelemetry_sdk() -> None:
    script = """
import os, sys, tempfile
workdir = tempfile.mkdtemp()
os.environ["DLBOARD_SQLITE_LOCATION"] = workdir + "/db.sqlite"
os.environ["DLBOARD_ARTIFACT_STORE_LOCATION"] = workdir + "/artifacts"
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.serve import app
from dlboard.serve._logging import configure_logging
configure_logging()
server = app([sqlite, filesystem, *LOCAL_AUTH, *BUILTIN_BACKEND]).server
assert server.test_client().post("/api/v1/metrics", json=[]).status_code == 200  # records a (no-op) metric
assert "/metrics" not in {rule.rule for rule in server.url_map.iter_rules()}
assert not [name for name in sys.modules if name.split(".")[0] in ("opentelemetry", "prometheus_client")]
print("ok")
"""
    result = _run_with_blocked_imports(_OPENTELEMETRY_PACKAGES, script)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "ok"
    assert "telemetry" not in (result.stdout + result.stderr).lower()  # no setup, no log spew


def test_turning_telemetry_on_without_the_otel_extra_is_a_startup_error() -> None:
    script = """
import os, sys
sys.modules["opentelemetry"] = None  # what an uninstalled package looks like to `find_spec`
os.environ["DLBOARD_TELEMETRY_METRICS"] = "prometheus"
from dlboard.serve.telemetry import TelemetrySettings
try:
    TelemetrySettings()
except ValueError as exc:
    print("dlboard[otel]" in str(exc))
"""
    result = _run_with_blocked_imports(_OPENTELEMETRY_PACKAGES, script)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"
