"""
`dlboard` resolves when its two distributions sit in separate `sys.path` entries (hermetic builds like Bazel).

`dlboard-client` ships `dlboard/__init__.py`; `dlboard` ships `dlboard/serve/` and friends with no
`__init__.py` of their own. In one site-packages the two merge on disk; given one `sys.path` entry
each, a regular package found first would hide the other's directory without `dlboard/__init__.py`
extending its `__path__`. Run in a subprocess for a clean import state, against the real
`dlboard/__init__.py` and a stand-in for the server's half of the tree.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import dlboard


@pytest.mark.parametrize("client_first", [True, False])
def test_the_server_half_imports_from_a_separate_path_entry(tmp_path: Path, *, client_first: bool) -> None:
    client_site, server_site = tmp_path / "client", tmp_path / "server"
    (client_site / "dlboard").mkdir(parents=True)
    shutil.copy(Path(dlboard.__file__), client_site / "dlboard" / "__init__.py")
    (server_site / "dlboard" / "serve").mkdir(parents=True)
    (server_site / "dlboard" / "serve" / "__init__.py").write_text("ORIGIN = 'server'\n")
    entries = [client_site, server_site] if client_first else [server_site, client_site]
    script = "import sys; sys.path[:0] = sys.argv[1:]; from dlboard.serve import ORIGIN; print(ORIGIN)"

    result = subprocess.run(
        [sys.executable, "-S", "-c", script, *map(str, entries)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "server"
