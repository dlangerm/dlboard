"""What produced a run: its code, how it was started, and the machine and environment it ran on."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import BaseModel, Field, StringConstraints

if TYPE_CHECKING:
    from dlboard.models._hparams import FlatHparamDict

_MAX_TEXT: Final = 4096
_Text = Annotated[str, StringConstraints(max_length=_MAX_TEXT)]
"""One fact: a commit, a path, a version. Bounded, as everything a client sends is."""

MAX_PACKAGES: Final = 5000
"""The most installed distributions a context lists -- far more than a real environment holds."""


class NewRunContext(BaseModel, frozen=True, extra="ignore"):
    """
    What a client recorded about a run as it started it. `extra="ignore"`: this crosses the wire -- see `dlboard._wire`.

    Every fact is optional: a client records what it could find (no git, a notebook with no command line) and what it
    was not told to leave out. A run keeps the first context recorded for it.
    """

    run_id: int
    experiment_id: int

    git_commit: str | None = Field(default=None, max_length=_MAX_TEXT)
    """The commit `HEAD` was at."""
    git_branch: str | None = Field(default=None, max_length=_MAX_TEXT)
    """The branch checked out; `None` on a detached `HEAD`."""
    git_remote: str | None = Field(default=None, max_length=_MAX_TEXT)
    """The `origin` remote's URL, with any credentials in it taken out."""
    git_dirty: bool | None = None
    """Whether tracked files had changes not committed; the changes themselves are the run's `context/git.diff`."""

    command: Annotated[list[_Text], Field(max_length=1000)] = Field(default_factory=list[str])
    """The command line (`sys.argv`), as the script saw it."""
    cwd: str | None = Field(default=None, max_length=_MAX_TEXT)
    executable: str | None = Field(default=None, max_length=_MAX_TEXT)
    """The Python interpreter that ran it."""
    python_version: str | None = Field(default=None, max_length=_MAX_TEXT)
    platform: str | None = Field(default=None, max_length=_MAX_TEXT)
    """`platform.platform()`: the OS, its release and the machine's architecture."""
    hostname: str | None = Field(default=None, max_length=_MAX_TEXT)

    packages: Annotated[dict[_Text, _Text], Field(max_length=MAX_PACKAGES)] = Field(
        default_factory=dict[str, str]
    )
    """Each installed distribution's version, by its normalized name."""
    gpus: Annotated[list[_Text], Field(max_length=64)] = Field(default_factory=list[str])
    """The name of each GPU the process could see, when CUDA was already up as the run started."""
    cuda_version: str | None = Field(default=None, max_length=_MAX_TEXT)
    """The CUDA version torch was built with, when torch was already imported."""

    def flat(self) -> FlatHparamDict:
        """
        One key per fact, as run compare lists it alongside a run's hyperparameters: `git/commit`, `packages/torch`, ...

        Facts not recorded are left out, rather than shown as an empty value that would differ from a run that has one.
        """
        facts: FlatHparamDict = {
            "git/commit": self.git_commit,
            "git/branch": self.git_branch,
            "git/remote": self.git_remote,
            "git/dirty": self.git_dirty,
            "command": shlex.join(self.command) if self.command else None,
            "cwd": self.cwd,
            "executable": self.executable,
            "python": self.python_version,
            "platform": self.platform,
            "host": self.hostname,
            "gpus": ", ".join(self.gpus) if self.gpus else None,
            "cuda": self.cuda_version,
            **{f"packages/{name}": version for name, version in sorted(self.packages.items())},
        }
        return {key: value for key, value in facts.items() if value is not None}


class RunContext(NewRunContext, frozen=True, extra="ignore"):
    """A run's context as stored. `extra="ignore"`: this crosses the wire -- see `dlboard._wire`."""

    id: int
