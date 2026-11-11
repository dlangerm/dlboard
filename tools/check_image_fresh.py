"""
Check that the `latest` image is the one built from the head of `main`, and file an issue while it is not.

`ci.yml` publishes `latest` and `sha-<commit>` together, so `latest` is current exactly when both tags resolve to the
same digest. The head gets `GRACE`, counted from its push run's start (a stack lands commits long after they were written), to publish (tests and a two-architecture build take the better part of an hour, and
a run of main waits for the one before it); past that, a missing or different `sha-` tag means the head's CI went red or
never finished -- the one failure `ci.yml`'s own report cannot see. Run by `.github/workflows/image-freshness.yml`
from a checkout of `main`. Exits 1 while the image is stale. Stdlib only; it calls `docker buildx` and `gh`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from typing import Final

IMAGE: Final = "ghcr.io/dlangerm/dlboard"
GRACE: Final = 2 * 60 * 60
"""Seconds a commit on `main` has to be published before its image is overdue."""
TITLE: Final = "The latest image is behind main"
"""The issue's title; an open issue with it is found by title, so a stale image is reported once."""
DIGEST: Final = re.compile(r"^Digest:\s+(\S+)", re.MULTILINE)


def run(*cmd: str, check: bool = True) -> str:
    """The stdout of `cmd`; a failing command raises unless `check` is off, so a broken `gh` is never read as "fine"."""
    return subprocess.run(cmd, capture_output=True, text=True, check=check).stdout


def digest(tag: str) -> str | None:
    """The digest `tag` resolves to, or None when there is no such tag."""
    found = DIGEST.search(run("docker", "buildx", "imagetools", "inspect", f"{IMAGE}:{tag}", check=False))
    return found.group(1) if found else None


def main(run_url: str) -> int:
    """Report the image's state, file or close the issue to match (`run_url` is this run, for the issue), and return the exit status."""
    head = run("git", "rev-parse", "--short=7", "HEAD").strip()
    # When the head landed, not when it was committed: a stack lands commits hours after it wrote them.
    landed = run(  # seconds since the head's push run started
        "gh",
        "run",
        "list",
        "--workflow",
        "ci.yml",
        "--branch",
        "main",
        "--event",
        "push",
        "--limit",
        "1",
        "--json",
        "createdAt",
        "--jq",
        "now - (.[0].createdAt | fromdateiso8601)",
    )
    age = float(landed)
    head_digest = digest(f"sha-{head}")
    stale = head_digest is None or head_digest != digest("latest")
    overdue = stale and age > GRACE
    filed = run(
        "gh",
        "issue",
        "list",
        "--state",
        "open",
        "--search",
        f"{TITLE} in:title",
        "--json",
        "number",
        "--jq",
        ".[].number",
    ).split()
    match (overdue, stale, filed):
        case (True, _, []):
            what = "missing" if head_digest is None else "a different image"
            body = (
                f"`{IMAGE}:latest` is not the image of `main`'s head `{head}`, which landed {age / 3600:.1f} hours ago "
                f'(`sha-{head}` is {what}). Found by {run_url}. Follow "When main is red" in CONTRIBUTING.md.'
            )
            run("gh", "issue", "create", "--title", TITLE, "--body", body)
        case (_, False, [*numbers]):
            for number in numbers:
                run("gh", "issue", "close", number, "--comment", f"`latest` is `main`'s head `{head}` again.")
        case _:
            pass
    sys.stdout.write(f"head {head}: latest is {'stale' if stale else 'current'} ({age / 3600:.1f}h old)\n")
    return int(overdue)


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
