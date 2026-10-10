#!/usr/bin/env python3
"""
Refuse a release-looking tag (`vX.Y.Z`, or `X.Y.Z`) that disagrees with `__version__` in the commit it points at.

Git has no pre-tag hook, but its `reference-transaction` hook runs before any ref changes and can veto the
transaction, `refs/tags/*` included. So this is installed as `.git/hooks/reference-transaction` (a symlink
to this file) and `git tag v0.5.0`, `git tag -f`, `git tag -a` and `git update-ref refs/tags/...`
fail up front, before a tag exists that `release.yml` would reject after it was pushed. Moving a pushed
tag is the painful part, so the check belongs here rather than in CI.

It reads the version from the *tagged commit*, not the working tree, so it also catches tagging the wrong
commit. It only acts when the git command that triggered it is one that creates tags by hand (`git tag`,
`git update-ref`): a tag that arrives through `git fetch`, `pull`, `clone` or `remote update` is somebody
else's, and a mismatched one on the remote must never block fetching. Deleting a tag is always allowed, as is
a tag on a commit with no `dlboard/_version.py`, and if the triggering command can't be identified the guard
lets the update through rather than risk blocking an unrelated one.

    tools/tag_guard.py --install    # symlink it into .git/hooks (the `install-tag-guard` prek hook does this)

Stdlib only and quick to start, since git runs it for every ref update (it returns at once unless a tag is
about to be created).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

VERSION_LIKE_TAG = re.compile(r"v?\d+\.\d+\.\d+")
"""
Tags that look like a release, `v` or not. `release.yml` only publishes `v*.*.*`, but a bare `0.4.1` is the
same mistake with the `v` forgotten, so it is held to the same rule: the tag must be `v<version>`.
"""
VERSION_FILE = "dlboard/_version.py"
HOOK_NAME = "reference-transaction"
TAGGING_COMMANDS = frozenset({"tag", "update-ref"})
"""The git subcommands that create a tag because you asked for one, as opposed to receiving one."""
_OPTIONS_WITH_A_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_VERSION_LINE = re.compile(r'^__version__ = "([^"]*)"', re.MULTILINE)


def _git(*args: str) -> str | None:
    """Stdout of `git <args>`, or None if it failed."""
    result = subprocess.run(["git", *args], check=False, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _triggering_subcommand() -> str | None:
    """The git subcommand whose ref update this hook is vetting (resolving an alias), or None if unknown."""
    parent = subprocess.run(
        ["ps", "-o", "args=", "-p", str(os.getppid())], check=False, capture_output=True, text=True
    ).stdout.split()
    words = iter(parent[1:] if parent and Path(parent[0]).name == "git" else [])
    for word in words:
        if word in _OPTIONS_WITH_A_VALUE:
            next(words, None)
        elif not word.startswith("-"):
            alias = _git("config", "--get", f"alias.{word}")
            return alias.split()[0] if alias else word
    return None


def mismatch(new_oid: str, ref: str) -> str | None:
    """Why creating or moving `ref` to `new_oid` should be refused, or None if it's fine."""
    tag = ref.removeprefix("refs/tags/")
    if tag == ref or not VERSION_LIKE_TAG.match(tag) or set(new_oid) == {"0"}:
        return None  # not a release tag, or a deletion
    commit = _git("rev-parse", "--verify", "--quiet", f"{new_oid}^{{commit}}")
    source = _git("show", f"{commit}:{VERSION_FILE}") if commit else None
    found = _VERSION_LINE.search(source) if source else None
    if commit is None or found is None or tag == f"v{found.group(1)}":
        return None  # not a commit, or there is no version to compare it with
    if _triggering_subcommand() not in TAGGING_COMMANDS:
        return None  # a tag that was fetched, not made by hand: not ours to refuse
    return (
        f"refusing to tag {tag}: {VERSION_FILE} at {commit[:10]} says {found.group(1)}\n"
        f"  tag it v{found.group(1)}, or bump {VERSION_FILE} and commit that first"
    )


def install() -> int:
    """Symlink this script in as the repo's `reference-transaction` hook, unless one is already there."""
    hooks_dir = _git("rev-parse", "--path-format=absolute", "--git-path", "hooks")
    if hooks_dir is None:
        sys.stderr.write("error: not in a git repository\n")
        return 1
    link, script = Path(hooks_dir) / HOOK_NAME, Path(__file__).resolve()
    if link.is_symlink() and link.resolve() == script:
        return 0
    if link.exists() or link.is_symlink():
        sys.stderr.write(f"error: {link} is already a different hook; remove it to use {script.name}\n")
        return 1
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(os.path.relpath(script, link.parent))
    sys.stdout.write(f"installed {link} -> {script}\n")
    return 0


def main(argv: list[str]) -> int:
    """Run as git's hook (`prepared <stdin: old new ref lines>`) or with `--install`."""
    if argv[1:] == ["--install"]:
        return install()
    if argv[1:2] != ["prepared"]:
        return 0  # the committed/aborted calls of the same transaction
    problems = [message for line in sys.stdin if (message := mismatch(*line.split()[1:]))]
    sys.stderr.write("".join(f"error: {problem}\n" for problem in problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
