"""
Keep `server/pyproject.toml`'s `dlboard-client` pin on the same major.minor as `dlboard/_version.py`.

The server wheel must pull in a `dlboard-client` of its own major.minor, so the two never skew in one
install. `server/pyproject.toml` stays a plain file uv edits; this rewrites its single
`"dlboard-client~=X.Y.Z"` dependency in place when X.Y drifted (a patch Z that is already right is left
alone), and fails when there isn't exactly one such dependency to rewrite.

Run by the `server-client-pin-matches-version` hook in `.pre-commit-config.yaml`; stdlib only.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
VERSION_FILE = ROOT / "dlboard" / "_version.py"
PYPROJECT = ROOT / "server" / "pyproject.toml"

_MAJOR_MINOR = re.compile(r'^__version__ = "(\d+\.\d+)\.', re.MULTILINE)
# Any quoted `"dlboard-client..."` requirement. The `[tool.uv.sources]` entry and comments aren't quoted
# requirements, so they don't count.
_REQUIREMENT = re.compile(r'^\s*"dlboard-client[^"]*"')


def _fail(message: str, *details: str) -> int:
    sys.stderr.write("\n".join([f"error: {message}", *(f"  {d}" for d in details)]) + "\n")
    return 1


def main() -> int:
    """Rewrite a drifted pin and return 0, or return 1 with a message when there is nothing safe to rewrite."""
    if (match := _MAJOR_MINOR.search(VERSION_FILE.read_text())) is None:
        return _fail(f"could not read a major.minor version from {VERSION_FILE.relative_to(ROOT)}")
    minor = match.group(1)
    pin = f'"dlboard-client~={minor}.0"'
    right_minor = re.compile(rf'"dlboard-client~={re.escape(minor)}\.\d+"')

    lines = PYPROJECT.read_text().splitlines(keepends=True)
    found = [(number, line) for number, line in enumerate(lines, 1) if _REQUIREMENT.match(line)]
    name = PYPROJECT.relative_to(ROOT)
    if not found:
        return _fail(f"{name} has no dlboard-client dependency", f"add {pin} to [project] dependencies")
    if len(found) > 1:
        return _fail(
            f"{name} lists dlboard-client {len(found)} times, it must be exactly once",
            *(f"{number}: {line.strip().rstrip(',')}" for number, line in found),
            f"keep only {pin}",
        )

    ((number, line),) = found
    if not right_minor.search(line):
        lines[number - 1] = re.sub(r'"dlboard-client[^"]*"', pin, line, count=1)
        PYPROJECT.write_text("".join(lines))
        sys.stdout.write(
            f"updated {name}:{number} from {line.strip().rstrip(',')} to {pin} to match "
            f"{VERSION_FILE.relative_to(ROOT)} (version {minor}); stage the change and commit again\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
