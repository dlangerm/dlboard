"""
Cut the next dlboard release.

Moves CHANGELOG.md's [Unreleased] notes under a new `## [X.Y.Z] - date` heading (leaving [Unreleased]
empty above it), bumps `__version__` in dlboard/_version.py, then runs prek.

    uv run python tools/bump_version.py minor    # 0.7.0 -> 0.8.0
    uv run python tools/bump_version.py major    # 0.7.0 -> 1.0.0

Nothing is committed or tagged: review the diff, commit it, then tag `vX.Y.Z` (release.yml checks the tag
against `__version__` and the changelog heading).

Needs the workspace environment (pendulum, pydantic), so it runs through `uv run`, not `--no-project`.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from enum import StrEnum
from pathlib import Path

import pendulum
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
VERSION_FILE = ROOT / "dlboard" / "_version.py"

UNRELEASED_HEADING = "## [Unreleased]"
VERSION_LINE = re.compile(r'^__version__ = "(\d+\.\d+\.\d+)"$', re.MULTILINE)


class BumpError(Exception):
    """A release that can't be cut from the repo as it stands."""


class Version(BaseModel):
    """A `major.minor.patch` release version."""

    model_config = ConfigDict(frozen=True)

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> Version:
        """Read `X.Y.Z`; pydantic rejects anything that isn't three integers."""
        major, minor, patch = text.split(".")
        return cls(major=major, minor=minor, patch=patch)

    def __str__(self) -> str:
        """Render as `X.Y.Z`."""
        return f"{self.major}.{self.minor}.{self.patch}"


class Bump(StrEnum):
    """Which part of the version to increment."""

    MINOR = "minor"
    MAJOR = "major"

    def apply(self, version: Version) -> Version:
        """Return the next version: minor resets patch, major resets minor and patch."""
        match self:
            case Bump.MINOR:
                return Version(major=version.major, minor=version.minor + 1, patch=0)
            case Bump.MAJOR:
                return Version(major=version.major + 1, minor=0, patch=0)


def read_version() -> Version:
    """Read the current `__version__` from dlboard/_version.py."""
    match = VERSION_LINE.search(VERSION_FILE.read_text())
    if match is None:
        message = f"can't find __version__ in {VERSION_FILE}"
        raise BumpError(message)
    return Version.parse(match[1])


def unreleased_body(lines: list[str]) -> list[str]:
    """Return the lines under `## [Unreleased]`, up to the next version heading."""
    headings = [i for i, line in enumerate(lines) if line.rstrip() == UNRELEASED_HEADING]
    if not headings:
        message = f"no '{UNRELEASED_HEADING}' heading in {CHANGELOG}"
        raise BumpError(message)
    start = headings[0]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## [")), len(lines))
    return lines[start + 1 : end]


def cut_changelog(text: str, version: Version, today: str) -> str:
    """Put a `## [version] - today` heading directly under `## [Unreleased]`, refusing an empty release."""
    if f"## [{version}]" in text:
        message = f"CHANGELOG.md already has a [{version}] section"
        raise BumpError(message)
    if not "".join(unreleased_body(text.splitlines(keepends=True))).strip():
        message = "[Unreleased] is empty; nothing to release"
        raise BumpError(message)
    heading = f"## [{version}] - {today}"
    return text.replace(f"{UNRELEASED_HEADING}\n", f"{UNRELEASED_HEADING}\n\n{heading}\n", 1)


def bump_version_file(text: str, version: Version) -> str:
    """Set `__version__` to `version` in the text of dlboard/_version.py."""
    return VERSION_LINE.sub(f'__version__ = "{version}"', text, count=1)


def main() -> int:
    """Cut the release and run prek over the tree; returns prek's exit code."""
    parser = argparse.ArgumentParser(description="Cut the next dlboard release.")
    parser.add_argument("bump", type=Bump, help="which part of the version to increment")
    args = parser.parse_args()

    try:
        current = read_version()
        next_version = args.bump.apply(current)
        today = pendulum.today().to_date_string()
        changelog = cut_changelog(CHANGELOG.read_text(), next_version, today)
    except BumpError as error:
        sys.stderr.write(f"error: {error}\n")
        return 1

    sys.stdout.write(f"Bumping {current} -> {next_version} ({today})\n")
    CHANGELOG.write_text(changelog)
    VERSION_FILE.write_text(bump_version_file(VERSION_FILE.read_text(), next_version))

    return subprocess.run(["uv", "run", "prek", "run", "--all-files"], cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
