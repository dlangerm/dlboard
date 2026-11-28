"""
What produced a run -- its code, how it was started, the machine and the environment -- collected as the run starts.

Collected in the training process (only it knows its command line and what it imported), by `Run.start` for a run it
creates. Nothing here fails or holds up a run for long: a fact that can't be found is left out, and every `git` call has
a short timeout. See `models.NewRunContext` for what is recorded.
"""

from __future__ import annotations

import importlib.metadata
import platform
import re
import socket
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard import models
from dlboard.client._warnings import warn
from dlboard.models import FILE_KIND_TAG, RUN_CONTEXT_DIFF_KEY, FileKind, NewArtifact, TagValue
from dlboard.models._run_context import MAX_PACKAGES

_GIT_TIMEOUT_SEC: Final = 5.0
_KB: Final = 1024


class ContextSettings(BaseSettings):
    """What a run records about what produced it (`DLBOARD_CONTEXT_*`). Everything but the diff is on by default."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_CONTEXT_", frozen=True)

    git: bool = True
    """Record the commit, the branch, the `origin` remote and whether tracked files had changes."""
    diff: bool = False
    """
    Upload those changes (`git diff HEAD`) as the run's `context/git.diff` artifact.

    Off unless asked for: changes not yet committed are where a secret pasted in to try something sits.
    """
    max_diff_kb: int = Field(default=1024, ge=0)
    """A bigger diff is left out (with a warning): it is usually a generated file, not code."""
    command: bool = True
    """Record the command line, the working directory and the interpreter. Turn it off if arguments carry secrets."""
    packages: bool = True
    """Record each installed distribution's version."""


class Diff(BaseModel, frozen=True, extra="forbid"):
    """A run's uncommitted changes, as an artifact: a `FileKind.DIFF` file the experiment page lists for download."""

    data: bytes
    key: str = RUN_CONTEXT_DIFF_KEY
    step: int = 0
    tags: Mapping[str, TagValue] = Field(default_factory=lambda: {FILE_KIND_TAG: FileKind.DIFF.value})

    def to_artifact(self, local_temp: Path, run_id: int, experiment_id: int) -> tuple[NewArtifact, Path]:
        """Write the diff under `local_temp`, to upload."""
        path = local_temp / Path(self.key).name
        path.write_bytes(self.data)
        new = NewArtifact(
            key=self.key,
            fname=path.name,
            run_id=run_id,
            experiment_id=experiment_id,
            step=self.step,
            tags=dict(self.tags),
        )
        return new, path


def _git(*args: str, most: int | None = None) -> bytes | None:
    """
    `git <args>`'s output in the working directory, or `None` if there is no git, no repository, or it failed.

    At most `most` bytes and one more of it are read, so an output over `most` is known to be without holding it all:
    it goes to a file, not a pipe, so `git` never waits on a reader that stopped.
    """
    try:
        with tempfile.TemporaryFile() as output:
            subprocess.run(
                ["git", *args],
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=_GIT_TIMEOUT_SEC,
                check=True,
            )
            output.seek(0)
            return output.read() if most is None else output.read(most + 1)
    except (OSError, subprocess.SubprocessError):
        return None


def _dirty() -> bool | None:
    """Whether tracked files have changes not committed, by `git diff --quiet`'s exit code; `None` if git can't say."""
    try:
        status = subprocess.run(
            ["git", "diff", "--quiet", "HEAD"], capture_output=True, timeout=_GIT_TIMEOUT_SEC, check=False
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return None
    match status:
        case 0:
            return False
        case 1:
            return True
        case _:
            return None


def _text(output: bytes | None) -> str | None:
    return (output.decode(errors="replace").strip() or None) if output is not None else None


def without_credentials(url: str) -> str:
    """`url` with any `user:token@` taken out, as a remote cloned over HTTPS with a token has it."""
    parts = urlsplit(url)
    if not parts.scheme or "@" not in parts.netloc:
        return url  # `git@host:org/repo.git` names a user, never a secret
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def _packages() -> dict[str, str]:
    """Every installed distribution's version, by its normalized name (PEP 503), the first found of each."""
    found: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        if name := distribution.metadata["Name"]:
            found.setdefault(re.sub(r"[-_.]+", "-", name).lower(), distribution.version)
    return dict(sorted(found.items())[:MAX_PACKAGES])


class _TorchCuda(Protocol):
    def is_initialized(self) -> bool:
        """Whether the script has started CUDA."""
        ...

    def device_count(self) -> int:
        """How many GPUs CUDA sees."""
        ...

    def get_device_name(self, index: int, /) -> str:
        """The name of GPU `index`."""
        ...


class _TorchVersion(Protocol):
    cuda: str | None


class _Torch(Protocol):
    """The little of torch's API read here -- the client never imports torch itself."""

    cuda: _TorchCuda
    version: _TorchVersion


def _cuda() -> tuple[list[str], str | None]:
    """
    The GPUs CUDA sees and the CUDA version torch was built with, if the script already imported torch.

    Never imports torch, and never starts CUDA: that would cost the script a CUDA context, and a process that forks its
    data loaders after starting CUDA breaks. The GPUs are named only once the script itself has started it.
    """
    torch: _Torch | None = cast("_Torch | None", sys.modules.get("torch"))
    if torch is None:
        return [], None
    try:
        cuda = torch.cuda
        gpus = (
            [cuda.get_device_name(index) for index in range(cuda.device_count())]
            if cuda.is_initialized()
            else list[str]()
        )
    except (AttributeError, RuntimeError):
        return [], None
    return gpus, torch.version.cuda


def collect(
    run_id: int, experiment_id: int, settings: ContextSettings
) -> tuple[models.NewRunContext, Diff | None]:
    """What produced this run, as far as `settings` allows; plus its uncommitted changes, when there are some to log."""
    commit = _text(_git("rev-parse", "HEAD")) if settings.git else None
    dirty = _dirty() if commit is not None else None
    remote = _text(_git("remote", "get-url", "origin")) if commit is not None else None
    gpus, cuda_version = _cuda()
    context = models.NewRunContext(
        run_id=run_id,
        experiment_id=experiment_id,
        git_commit=commit,
        git_branch=_text(_git("symbolic-ref", "--short", "-q", "HEAD")) if commit is not None else None,
        git_remote=None if remote is None else without_credentials(remote),
        git_dirty=dirty,
        command=list(sys.argv) if settings.command else [],
        cwd=str(Path.cwd()) if settings.command else None,
        executable=sys.executable if settings.command else None,
        python_version=platform.python_version(),
        platform=platform.platform(),
        hostname=socket.gethostname(),
        packages=_packages() if settings.packages else {},
        gpus=gpus,
        cuda_version=cuda_version,
    )
    return context, _diff(settings) if dirty else None


def _diff(settings: ContextSettings) -> Diff | None:
    """The changes to upload, or `None`: not asked for, gone, or over `max_diff_kb` (which is warned about)."""
    most = settings.max_diff_kb * _KB
    changes = _git("diff", "HEAD", "--no-color", "--no-ext-diff", most=most) if settings.diff else None
    if not changes:
        return None
    if len(changes) > most:
        warn(
            "dlboard left this run's uncommitted changes out: the diff is over "
            f"DLBOARD_CONTEXT_MAX_DIFF_KB ({settings.max_diff_kb})."
        )
        return None
    return Diff(data=changes)
