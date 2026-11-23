"""
Find the flaky tests among the ones the full suite just failed: rerun each failed test on its own, several times.

A test the suite failed and a rerun passes is flaky: the script exits 1, and with `--file-issues` it opens a GitHub
issue for each flaky test that doesn't already have one open. A test that fails every rerun is not flaky, it is
failing, and the suite's own report stands, so the script exits 0 for it. Each rerun is its own pytest process, so no
state carries over from the suite's run or from one rerun to the next -- except Hypothesis's examples (`.hypothesis/`).

A property test that fails on an example Hypothesis drew is replayed on that example by every rerun, so it fails each
one: that is an edge case nobody wrote a test for, not a flaky test. It exits 1 and, with `--file-issues`, gets an issue
of its own that quotes the example.

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
from typing import Final, NamedTuple

ROOT = Path(__file__).parents[1]
LAST_FAILED: Final = ROOT / ".pytest_cache" / "v" / "cache" / "lastfailed"
"""pytest's record of the tests that failed, keyed by node id; it accumulates over the runs of one checkout."""
REPEATS: Final = 3
"""How many times each failed test is rerun."""
RECHECKED_AT_MOST: Final = 10
"""A suite that fails more tests than this is broken, not flaky; the rest are left to its own report."""
NO_TESTS_SELECTED: Final = 5
"""pytest's exit status when it selects no test."""
FAILING_CASE: Final = "Failing test case: "
"""What Hypothesis prints before the example a property test failed on."""
EXAMPLE_CHARS: Final = 2000
"""How much of a failing run's output, from `FAILING_CASE` on, an edge case's issue quotes."""
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
    SKIP = "skip"


class Verdict(StrEnum):
    """What a test's runs add up to."""

    STABLE_FAIL = "stable fail"
    FLAKY = "flaky"
    EDGE_CASE = "edge case"
    NOT_RERUNNABLE = "not rerunnable"

    @property
    def title_prefix(self) -> str:
        """Start of the title of the issue filed for it; the rest is the node id, so a repeat is found by title."""
        match self:
            case Verdict.FLAKY:
                return "Flaky test: "
            case Verdict.EDGE_CASE:
                return "Edge case: "
            case Verdict.STABLE_FAIL | Verdict.NOT_RERUNNABLE:
                return f"{self.value.capitalize()}: "


class Run(NamedTuple):
    """How one run of a test ended, and the end of its output when it failed."""

    outcome: Outcome
    output: str = ""


def verdict(runs: list[Run]) -> Verdict:
    """
    What a test the suite failed and its reruns add up to.

    Flaky when any rerun passes. A test that fails every run is failing, not flaky: an edge case when Hypothesis found
    the example it fails on. One that is skipped on its own (a check that needs the whole suite collected) says nothing
    either way.
    """
    outcomes = {run.outcome for run in runs}
    if Outcome.SKIP in outcomes:
        return Verdict.NOT_RERUNNABLE
    if Outcome.PASS in outcomes:
        return Verdict.FLAKY
    return Verdict.EDGE_CASE if any(FAILING_CASE in run.output for run in runs) else Verdict.STABLE_FAIL


def run_once(node_id: str) -> Run:
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
        return Run(Outcome.PASS if " passed" in result.stdout else Outcome.SKIP)
    output = result.stdout + result.stderr
    sys.stdout.write(f"--- {node_id} failed; end of its output:\n{output[-OUTPUT_TAIL:]}\n")
    return Run(Outcome.FAIL, output)


def gh(*args: str) -> str:
    """Run the GitHub CLI and return what it printed."""
    return subprocess.run(["gh", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def file_issue(node_id: str, found: Verdict, runs: list[Run], run_url: str | None) -> None:
    """Open an issue for a flaky test or an edge case, unless an open one already has its title."""
    title = f"{found.title_prefix}{node_id}"
    listed = json.loads(gh("issue", "list", "--state", "open", "--limit", "1000", "--json", "title"))
    if title in {issue["title"] for issue in listed}:
        sys.stdout.write(f"an open issue already covers {node_id}\n")
        return
    match found:
        case Verdict.EDGE_CASE:
            output = next(run.output for run in runs if FAILING_CASE in run.output)
            example = output[output.index(FAILING_CASE) :][:EXAMPLE_CHARS]
            explained = [
                "A property test failed on an example Hypothesis drew, and every rerun failed on it again:",
                "",
                f"```\n{example}\n```",
                "",
                "Fix what it found, then keep the case in the test with `@example(...)`, so it is tried on every run.",
            ]
        case Verdict.FLAKY | Verdict.STABLE_FAIL | Verdict.NOT_RERUNNABLE:
            explained = [
                (
                    "The full suite failed this test, and a rerun on its own passed. Reproduce it on its own with "
                    f"`uv run pytest '{node_id}'` a few times; if it only fails after other tests, one of them leaks "
                    "state."
                )
            ]
    body = "\n".join(
        [
            f"Found by the flaky-tests check in {run_url or 'a local run'}.",
            "",
            f"- Test: `{node_id}`",
            f"- Runs, in order (the first is the suite's): {', '.join(run.outcome.value for run in runs)}",
            "",
            *explained,
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

    results = {
        node_id: [Run(Outcome.FAIL), *(run_once(node_id) for _ in range(REPEATS))] for node_id in rechecked
    }
    verdicts = {node_id: verdict(runs) for node_id, runs in results.items()}
    for node_id, runs in results.items():
        outcomes = " ".join(run.outcome.value for run in runs)
        sys.stdout.write(f"{verdicts[node_id].value:14} {outcomes:19} {node_id}\n")

    reported = {
        node_id: found for node_id, found in verdicts.items() if found in {Verdict.FLAKY, Verdict.EDGE_CASE}
    }
    if args.file_issues:
        for node_id, found in reported.items():
            file_issue(node_id, found, results[node_id], args.run_url)
    for found in (Verdict.FLAKY, Verdict.EDGE_CASE):
        sys.stdout.write(f"{list(reported.values()).count(found)} {found.value}(s) among the failed tests\n")
    return int(bool(reported))


if __name__ == "__main__":
    sys.exit(main())
