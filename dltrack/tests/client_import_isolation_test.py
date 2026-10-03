"""
A client install must never need the server stack just to log a run.

Run in a subprocess, not in-process: this test suite's own `conftest.py` imports `dash` (and
friends) long before any test function runs, so by the time a test body executes, checking
`sys.modules` here would prove nothing. A real client-only install (`pip install dltrack`, no
`[server]` extra) doesn't have dash, flask, sqlalchemy, pandas, granian or dash-mantine-components/
dash-ag-grid installed at all -- this simulates that by making every import of one of them fail,
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

_SCRIPT = f"""
import builtins
_blocked = {_SERVER_ONLY_PACKAGES!r}
_real_import = builtins.__import__
def _guarded(name, *a, **kw):
    if name.split(".")[0] in _blocked:
        raise ImportError(f"simulated: {{name}} not installed")
    return _real_import(name, *a, **kw)
builtins.__import__ = _guarded

from dltrack.client import DLTrackLogger, DLTrackLoggerSettings
from dltrack.client._rest_api import BasicDltrackAPI
from dltrack.client.artifacts.image import Image
from dltrack.client.artifacts.link import Link
print("ok")
"""


def test_importing_the_client_never_needs_the_server_stack() -> None:
    result = subprocess.run([sys.executable, "-c", _SCRIPT], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
