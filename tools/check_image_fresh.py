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
import shlex
import subprocess
import sys
from enum import StrEnum
from typing import Final

IMAGE: Final = "ghcr.io/dlangerm/dlboard"
GRACE: Final = 2 * 60 * 60
"""Seconds a commit on `main` has to be published before its image is overdue."""
TITLE: Final = "The latest image is behind main"
"""The issue's title; an open issue with it is found by title, so a stale image is reported once."""
LAST_PUSH: Final = shlex.split(
    "gh run list --workflow ci.yml --branch main --event push --limit 1 --json createdAt --jq"
)
"""The run of `main`'s head; a `--jq` expression follows."""
OPEN_ISSUES: Final = shlex.split("gh issue list --state open --search")
"""The open issues matching a search that follows."""
DIGEST: Final = re.compile(r"^Digest:\s+(\S+)", re.MULTILINE)


class State(StrEnum):
    """How `latest` stands against the head of `main`."""

    CURRENT = "current"
    PENDING = "pending"
    """No image of the head yet, but it landed less than `GRACE` ago."""
    OVERDUE = "overdue"


def digest(tag: str) -> str | None:
    """The digest `tag` resolves to, or None when there is no such tag."""
    inspected = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", f"{IMAGE}:{tag}"],
        capture_output=True,
        text=True,
        check=False,
    )
    found = DIGEST.search(inspected.stdout)
    return found.group(1) if found else None


def main(run_url: str) -> int:
    """Report the image's state, file or close the issue to match (`run_url` is this run, for the issue)."""
    head = subprocess.run(
        ["git", "rev-parse", "--short=7", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    # When the head landed, not when it was committed: a stack lands commits hours after it wrote them.
    age = float(
        subprocess.run(
            [*LAST_PUSH, "now - (.[0].createdAt | fromdateiso8601)"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    head_digest = digest(f"sha-{head}")
    if head_digest is not None and head_digest == digest("latest"):
        state = State.CURRENT
    else:
        state = State.OVERDUE if age > GRACE else State.PENDING
    filed = subprocess.run(
        [*OPEN_ISSUES, f"{TITLE} in:title", "--json", "number", "--jq", ".[].number"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    match state:
        case State.OVERDUE if not filed:
            what = "missing" if head_digest is None else "a different image"
            body = (
                f"`{IMAGE}:latest` is not the image of `main`'s head `{head}`, which landed {age / 3600:.1f} hours ago "
                f'(`sha-{head}` is {what}). Found by {run_url}. Follow "When main is red" in CONTRIBUTING.md.'
            )
            subprocess.run(["gh", "issue", "create", "--title", TITLE, "--body", body], check=True)
        case State.OVERDUE:
            pass  # already reported
        case State.PENDING:
            pass  # still within GRACE
        case State.CURRENT:
            for number in filed:
                subprocess.run(
                    [
                        "gh",
                        "issue",
                        "close",
                        number,
                        "--comment",
                        f"`latest` is `main`'s head `{head}` again.",
                    ],
                    check=True,
                )
    sys.stdout.write(f"head {head}: latest is {state} ({age / 3600:.1f}h since it landed)\n")
    return int(state is State.OVERDUE)


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
