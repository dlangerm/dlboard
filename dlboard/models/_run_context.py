"""What produced a run: its code, how it was started, and the machine and environment it ran on."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import BaseModel, Field, StringConstraints

from dlboard._compat import StrEnum

if TYPE_CHECKING:
    from dlboard.models._hparams import FlatHparamDict

_MAX_TEXT: Final = 4096
_Text = Annotated[str, StringConstraints(max_length=_MAX_TEXT)]
"""One fact: a commit, a path, a version. Bounded, as everything a client sends is."""

MAX_PACKAGES: Final = 5000
"""The most installed distributions a context lists -- far more than a real environment holds."""

RUN_CONTEXT_DIFF_KEY: Final = "context/git.diff"
"""The artifact a run's uncommitted changes are logged as, beside its context."""


class ContextSection(StrEnum):
    """What a fact of a run's context is about."""

    GIT = "git"
    """The commit, branch and remote, and whether there were changes not committed."""
    COMMAND = "command"
    """How the run was started: its command line, working directory and interpreter."""
    MACHINE = "machine"
    """Where it ran: Python, the platform, the host, CUDA and the GPUs."""
    PACKAGES = "packages"
    """Each installed distribution's version."""


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

    def sections(self) -> dict[ContextSection, FlatHparamDict]:
        """
        The facts recorded, by what each is about, a key each: `git/commit`, `command`, `packages/torch`, ...

        Facts not recorded are left out, rather than shown as an empty value that would differ from a run that has one.
        """
        sections: dict[ContextSection, FlatHparamDict] = {
            ContextSection.GIT: {
                "git/commit": self.git_commit,
                "git/branch": self.git_branch,
                "git/remote": self.git_remote,
                "git/dirty": self.git_dirty,
            },
            ContextSection.COMMAND: {
                "command": shlex.join(self.command) if self.command else None,
                "cwd": self.cwd,
                "executable": self.executable,
            },
            ContextSection.MACHINE: {
                "python": self.python_version,
                "platform": self.platform,
                "host": self.hostname,
                "gpus": ", ".join(self.gpus) if self.gpus else None,
                "cuda": self.cuda_version,
            },
            ContextSection.PACKAGES: {
                f"packages/{name}": version for name, version in sorted(self.packages.items())
            },
        }
        return {
            section: {key: value for key, value in facts.items() if value is not None}
            for section, facts in sections.items()
        }

    def flat(self) -> FlatHparamDict:
        """Every fact recorded, as run compare lists them alongside a run's hyperparameters (see `sections`)."""
        return {key: value for facts in self.sections().values() for key, value in facts.items()}


class RunContext(NewRunContext, frozen=True, extra="ignore"):
    """A run's context as stored. `extra="ignore"`: this crosses the wire -- see `dlboard._wire`."""

    id: int
