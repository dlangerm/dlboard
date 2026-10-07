"""
Both distributions actually build and install together -- not just that their config looks right.

Regression coverage for a real bug this caught: `uv build`'s default output is a wheel *and* an
sdist, but each `hatch_build.py` hook only ran for the wheel target -- an `sdist` built `dlboard-client`'s
or `dlboard`'s own `pyproject.toml`/`hatch_build.py` and nothing else, since those are the
only files actually inside `client/`/`server/`. A wheel built from that sdist (as a release
pipeline, or a conda-forge/Linux-distro rebuild, would) silently shipped empty (client) or failed
outright (server, missing `dlboard/_cli.py`). This builds an sdist for each distribution, builds a
wheel from *that* sdist (not straight from the repo -- the case that was actually broken), installs
both into a throwaway venv, and imports from it and runs its `dlboard` entry point, the same way
`CLAUDE.md` says this has to be verified.
"""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

from dlboard._version import __version__

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO_ROOT = Path(__file__).parents[2]


def _run(args: Sequence[str], *, cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _build_wheel_from_a_fresh_sdist(project_dir: Path, tmp_path: Path, label: str) -> Path:
    """Build `project_dir`'s sdist, extract it, then build a wheel from *that* extracted copy."""
    sdist_out = tmp_path / f"{label}-sdist"
    _run(["uv", "build", "--sdist", "--out-dir", str(sdist_out)], cwd=project_dir)
    (sdist_archive,) = sdist_out.glob("*.tar.gz")

    extracted = tmp_path / f"{label}-extracted"
    with tarfile.open(sdist_archive) as tar:
        tar.extractall(extracted, filter="data")  # our own just-built sdist, not untrusted input
    (extracted_project,) = extracted.iterdir()

    wheel_out = tmp_path / f"{label}-wheel"
    _run(["uv", "build", "--wheel", "--out-dir", str(wheel_out)], cwd=extracted_project)
    (wheel,) = wheel_out.glob("*.whl")
    return wheel


def test_both_distributions_build_from_their_own_sdist_and_install_together(tmp_path: Path) -> None:
    client_wheel = _build_wheel_from_a_fresh_sdist(REPO_ROOT / "client", tmp_path, "client")
    server_wheel = _build_wheel_from_a_fresh_sdist(REPO_ROOT / "server", tmp_path, "server")

    venv = tmp_path / "venv"
    _run(["uv", "venv", str(venv)])
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv / "bin" / "python"),
            str(client_wheel),
            str(server_wheel),
        ]
    )

    # Importing from the *installed* venv, not this process -- which already has every dependency
    # (including dash/sqlalchemy) loaded from the dev environment and would prove nothing. Also not
    # run from the repo root, which would shadow the installed package with the source tree itself.
    imports = subprocess.run(
        [
            str(venv / "bin" / "python"),
            "-c",
            "import dlboard, dlboard._cli, dlboard._version, dlboard.client, dlboard.plugins, dlboard.serve.app",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert imports.returncode == 0, imports.stderr

    version = subprocess.run(
        [str(venv / "bin" / "dlboard"), "--version"], capture_output=True, text=True, check=False
    )
    assert version.returncode == 0
    assert version.stdout.strip() == __version__
