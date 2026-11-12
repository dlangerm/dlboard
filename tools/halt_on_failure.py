"""
Cancel the rest of a pull request's CI run once one of its jobs has failed, leaving that job shown as the one that failed.

A job that cancels its own run is cancelled with it, so a run halted from inside the failing job showed every job
cancelled and none failed. This runs as a job of its own beside the others (`halt-on-failure` in `ci.yml`) and cancels
the run only once a failed job has finished: a finished job keeps its conclusion through a cancel. It ends when every
other job has. Never fails itself, so the only failure a run shows is the job that failed. Stdlib only; it calls `gh`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Final

POLL: Final = 15
"""Seconds between looks at the run's jobs: how long a failure can leave the others running before they are cancelled."""
FAILED: Final = frozenset({"failure", "timed_out"})
"""The conclusions of a job that failed (a cancelled one did not: it was stopped)."""


@dataclass(frozen=True)
class Job:
    """One job of the run, as far as halting it goes."""

    name: str
    finished: bool
    failed: bool


def run_jobs(repo: str, run_id: str, attempt: str) -> list[Job] | None:
    """The jobs of `attempt` of the run, or `None` when GitHub could not be asked this time (the next look asks again)."""
    found = subprocess.run(
        [
            *("gh", "api", "--paginate", f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs"),
            *("--jq", ".jobs[] | {name, status, conclusion} | tojson"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if found.returncode != 0:
        sys.stdout.write(f"::warning::could not list the run's jobs: {found.stderr.strip()}\n")
        return None
    rows = [json.loads(line) for line in found.stdout.splitlines()]
    return [Job(row["name"], row["status"] == "completed", row["conclusion"] in FAILED) for row in rows]


def main(repo: str, run_id: str, attempt: str, me: str) -> int:
    """Watch run `run_id`'s `attempt` of `repo` from job `me` until a job fails (then cancel the run) or all are done."""
    while True:
        time.sleep(POLL)
        jobs = run_jobs(repo, run_id, attempt)
        if jobs is None:
            continue
        others = [job for job in jobs if job.name != me]
        if failed := [job.name for job in others if job.failed]:
            sys.stdout.write(f"::notice::{', '.join(failed)} failed: cancelling the rest of the run\n")
            sys.stdout.flush()
            if subprocess.run(["gh", "run", "cancel", run_id, "--repo", repo], check=False).returncode != 0:
                sys.stdout.write("::warning::could not cancel the run\n")
            return 0
        if all(job.finished for job in others):
            return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
