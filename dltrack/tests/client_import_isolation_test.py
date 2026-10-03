"""
Neither side of dltrack needs the other's heavy dependencies just to be imported.

Run in a subprocess, not in-process: this test suite's own `conftest.py` imports `dash` (and
`torch`, transitively via `pytorch_lightning`) long before any test function runs, so by the time
a test body executes, checking `sys.modules` here would prove nothing. A real client-only install
(`pip install dltrack`, no `[server]` extra) doesn't have dash, flask, sqlalchemy, pandas, granian
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
from dltrack.client import DLTrackLogger, DLTrackLoggerSettings
from dltrack.client._rest_api import BasicDltrackAPI
from dltrack.client.artifacts.image import Image
from dltrack.client.artifacts.link import Link
print("ok")
"""
    result = _run_with_blocked_imports(_SERVER_ONLY_PACKAGES, script)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_importing_the_server_never_needs_torch() -> None:
    script = """
import dltrack.plugins.backend.basic_rest_backend
import dltrack.serve.app
print("ok")
"""
    result = _run_with_blocked_imports(_CLIENT_ONLY_PACKAGES, script)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
