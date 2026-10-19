"""
Gate the full test suite on its flaky tests: run a random sample of the suite's tests, each several times in a row.

The sample is drawn from every collected test. Its seed is printed, so a run's sample can be reproduced with `--seed`.
Each run is its own pytest process, so no state carries over from one run to the next. A test whose runs disagree
(some pass, some fail) is flaky: the script exits 1, and with `--file-issues` it opens a GitHub issue for each flaky
test that doesn't already have one open. A test that fails every run is not flaky, it is failing, and the full suite
reports it, so the script exits 0 for it.

Run by the `flaky-tests` job in `.github/workflows/ci.yml`, ahead of the suite. Stdlib only; it calls `uv run pytest`
and `gh`.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import Final

ROOT = Path(__file__).parents[1]
SAMPLE_SIZE: Final = 5
"""How many tests a run samples."""
REPEATS: Final = 3
"""How many times in a row each sampled test runs."""
NO_TESTS_SELECTED: Final = 5
"""pytest's exit status when it selects no test."""
TITLE_PREFIX: Final = "Flaky test: "
"""Start of the title of the issue filed for a flaky test; the rest is its node id, so a repeat is found by title."""
NODE_ID: Final = re.compile(r"^\S+\.py::.+$")
"""A line of `pytest --collect-only -q` that names a test."""
OUTPUT_TAIL: Final = 3000
"""How many characters of a failing run's output to show."""


class Outcome(StrEnum):
    """How one run of a test ended."""

    PASS = "pass"
    FAIL = "fail"


class Verdict(StrEnum):
    """What a test's runs add up to."""

    STABLE_PASS = "stable pass"
    STABLE_FAIL = "stable fail"
    FLAKY = "flaky"


def verdict(outcomes: list[Outcome]) -> Verdict:
    """A test is flaky when its runs disagree; one that fails every run is failing, not flaky."""
    passed = sum(outcome is Outcome.PASS for outcome in outcomes)
    if passed == len(outcomes):
        return Verdict.STABLE_PASS
    if passed == 0:
        return Verdict.STABLE_FAIL
    return Verdict.FLAKY


def collect(match: str) -> list[str]:
    """Every collected test whose node id contains `match`, in collection order."""
    result = subprocess.run(
        ["uv", "run", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        sys.stderr.write(result.stdout + result.stderr)
        sys.exit(f"error: collecting the suite failed with status {result.returncode}")
    return [line for line in result.stdout.splitlines() if NODE_ID.match(line) and match in line]


def run_once(node_id: str) -> Outcome:
    """Run one test in its own pytest process, showing the end of its output when it fails."""
    result = subprocess.run(
        ["uv", "run", "pytest", node_id, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode == NO_TESTS_SELECTED:
        sys.exit(f"error: pytest selected no test for {node_id}")
    if result.returncode == 0:
        return Outcome.PASS
    tail = (result.stdout + result.stderr)[-OUTPUT_TAIL:]
    sys.stdout.write(f"--- {node_id} failed; end of its output:\n{tail}\n")
    return Outcome.FAIL


def gh(*args: str) -> str:
    """Run the GitHub CLI and return what it printed."""
    return subprocess.run(["gh", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def file_issue(node_id: str, outcomes: list[Outcome], seed: int, run_url: str | None) -> None:
    """Open an issue for a flaky test, unless an open one already has its title."""
    title = f"{TITLE_PREFIX}{node_id}"
    listed = json.loads(gh("issue", "list", "--state", "open", "--limit", "1000", "--json", "title"))
    if title in {issue["title"] for issue in listed}:
        sys.stdout.write(f"an open issue already covers {node_id}\n")
        return
    body = "\n".join(
        [
            f"Found by the flaky-tests job in {run_url or 'a local run'}.",
            "",
            f"- Test: `{node_id}`",
            f"- Runs, in order: {', '.join(outcome.value for outcome in outcomes)}",
            f"- Sample seed: {seed}",
            "",
            (
                "Some runs of this test passed and others failed. Reproduce it on its own with "
                f"`uv run pytest '{node_id}'` a few times."
            ),
        ]
    )
    url = gh("issue", "create", "--title", title, "--body", body).strip()
    sys.stdout.write(f"filed {url} for {node_id}\n")


def main() -> int:
    """Sample, run, and report; return 1 when the sample holds a flaky test."""
    parser = argparse.ArgumentParser(
        description="Run a random sample of the suite's tests several times each."
    )
    parser.add_argument("--seed", type=int, help="the sample of a previous run, as its seed")
    parser.add_argument("--match", default="", help="only sample tests whose node id contains this text")
    parser.add_argument("--file-issues", action="store_true", help="open an issue for each flaky test")
    parser.add_argument("--run-url", help="this CI run's link, for the issue body")
    args = parser.parse_args()

    pool = collect(args.match)
    if not pool:
        sys.exit("error: no test to sample")
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
    sample = random.Random(seed).sample(pool, min(SAMPLE_SIZE, len(pool)))
    sys.stdout.write(f"seed {seed}: {len(sample)} of {len(pool)} tests, each run {REPEATS} times in a row\n")

    results = {node_id: [run_once(node_id) for _ in range(REPEATS)] for node_id in sample}
    for node_id, outcomes in results.items():
        runs = " ".join(outcome.value for outcome in outcomes)
        sys.stdout.write(f"{verdict(outcomes).value:12} {runs:14} {node_id}\n")

    flaky = [node_id for node_id, outcomes in results.items() if verdict(outcomes) is Verdict.FLAKY]
    if not flaky:
        sys.stdout.write("no flaky tests in the sample\n")
        return 0
    if args.file_issues:
        for node_id in flaky:
            file_issue(node_id, results[node_id], seed, args.run_url)
    sys.stdout.write(f"{len(flaky)} flaky test(s) in the sample; rerun this sample with --seed {seed}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
