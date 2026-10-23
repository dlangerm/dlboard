"""
Find the flaky tests among the ones the full suite just failed: rerun each failed test on its own, several times.

A test the suite failed and a rerun passes is flaky: the script exits 1, and with `--file-issues` it opens a GitHub
issue for each flaky test that doesn't already have one open. A test that fails every rerun is not flaky, it is
failing, and the suite's own report stands, so the script exits 0 for it. Each rerun is its own pytest process, so no
state carries over from the suite's run or from one rerun to the next.

The failed tests are read from pytest's cache (`lastfailed`), which every pytest run of a CI job adds to. Run by the
jobs of `.github/workflows/ci.yml` that run the suite, when the suite fails (`.github/actions/recheck-failures`).
Stdlib only; it calls `uv run pytest` and `gh`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import Final

ROOT = Path(__file__).parents[1]
LAST_FAILED: Final = ROOT / ".pytest_cache" / "v" / "cache" / "lastfailed"
"""pytest's record of the tests that failed, keyed by node id; it accumulates over the runs of one checkout."""
REPEATS: Final = 3
"""How many times each failed test is rerun."""
RECHECKED_AT_MOST: Final = 10
"""A suite that fails more tests than this is broken, not flaky; the rest are left to its own report."""
NO_TESTS_SELECTED: Final = 5
"""pytest's exit status when it selects no test."""
TITLE_PREFIX: Final = "Flaky test: "
"""Start of the title of the issue filed for a flaky test; the rest is its node id, so a repeat is found by title."""
NODE_ID: Final = re.compile(r"^\S+\.py::.+$")
"""A test's node id; a failed collection is recorded under its file alone."""
SCREENSHOTS_FILE: Final = "docs_screenshots_test.py"
"""Its tests are skipped unless a run asks for `--screenshots`."""
OUTPUT_TAIL: Final = 3000
"""How many characters of a failing run's output to show."""


class Outcome(StrEnum):
    """How one run of a test ended."""

    PASS = "pass"
    FAIL = "fail"


class Verdict(StrEnum):
    """What a test's runs add up to."""

    STABLE_FAIL = "stable fail"
    FLAKY = "flaky"


def verdict(outcomes: list[Outcome]) -> Verdict:
    """A test the suite failed is flaky when any rerun passes; one that fails every run is failing, not flaky."""
    return Verdict.FLAKY if Outcome.PASS in outcomes else Verdict.STABLE_FAIL


def run_once(node_id: str) -> Outcome:
    """Run one test in its own pytest process, showing the end of its output when it fails."""
    screenshots = ["--screenshots=check"] if SCREENSHOTS_FILE in node_id else []
    result = subprocess.run(
        ["uv", "run", "pytest", node_id, "-q", "-p", "no:cacheprovider", *screenshots],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode == NO_TESTS_SELECTED:
        sys.exit(f"error: pytest selected no test for {node_id}")
    if result.returncode == 0:
        if " passed" not in result.stdout:
            sys.exit(f"error: {node_id} was skipped, so it says nothing about the failure")
        return Outcome.PASS
    tail = (result.stdout + result.stderr)[-OUTPUT_TAIL:]
    sys.stdout.write(f"--- {node_id} failed; end of its output:\n{tail}\n")
    return Outcome.FAIL


def gh(*args: str) -> str:
    """Run the GitHub CLI and return what it printed."""
    return subprocess.run(["gh", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def file_issue(node_id: str, outcomes: list[Outcome], run_url: str | None) -> None:
    """Open an issue for a flaky test, unless an open one already has its title."""
    title = f"{TITLE_PREFIX}{node_id}"
    listed = json.loads(gh("issue", "list", "--state", "open", "--limit", "1000", "--json", "title"))
    if title in {issue["title"] for issue in listed}:
        sys.stdout.write(f"an open issue already covers {node_id}\n")
        return
    body = "\n".join(
        [
            f"Found by the flaky-tests check in {run_url or 'a local run'}.",
            "",
            f"- Test: `{node_id}`",
            f"- Runs, in order (the first is the suite's): {', '.join(outcome.value for outcome in outcomes)}",
            "",
            (
                "The full suite failed this test, and a rerun on its own passed. Reproduce it on its own with "
                f"`uv run pytest '{node_id}'` a few times; if it only fails after other tests, one of them leaks state."
            ),
        ]
    )
    url = gh("issue", "create", "--title", title, "--body", body).strip()
    sys.stdout.write(f"filed {url} for {node_id}\n")


def main() -> int:
    """Rerun the tests the suite failed and report; return 1 when one of them is flaky."""
    parser = argparse.ArgumentParser(
        description="Rerun the tests the full suite failed, to find the flaky ones."
    )
    parser.add_argument(
        "--last-failed", type=Path, default=LAST_FAILED, help="pytest's `lastfailed` cache file"
    )
    parser.add_argument("--file-issues", action="store_true", help="open an issue for each flaky test")
    parser.add_argument("--run-url", help="this CI run's link, for the issue body")
    args = parser.parse_args()

    recorded = json.loads(args.last_failed.read_text()) if args.last_failed.exists() else {}
    failed = sorted(key for key in recorded if NODE_ID.match(key))
    if not failed:
        sys.stdout.write("the suite recorded no failed test to rerun\n")
        return 0
    rechecked = failed[:RECHECKED_AT_MOST]
    sys.stdout.write(f"rerunning {len(rechecked)} of {len(failed)} failed tests, {REPEATS} times each\n")

    results = {node_id: [Outcome.FAIL, *(run_once(node_id) for _ in range(REPEATS))] for node_id in rechecked}
    for node_id, outcomes in results.items():
        runs = " ".join(outcome.value for outcome in outcomes)
        sys.stdout.write(f"{verdict(outcomes).value:12} {runs:19} {node_id}\n")

    flaky = [node_id for node_id, outcomes in results.items() if verdict(outcomes) is Verdict.FLAKY]
    if args.file_issues:
        for node_id in flaky:
            file_issue(node_id, results[node_id], args.run_url)
    sys.stdout.write(f"{len(flaky)} flaky test(s) among the failed ones\n")
    return int(bool(flaky))


if __name__ == "__main__":
    sys.exit(main())
