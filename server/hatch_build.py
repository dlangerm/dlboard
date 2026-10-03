"""
Hatchling build hook: pull the server's code out of the shared `../dltrack` source tree.

`dltrack-server`'s own files (`dltrack/serve/`, `dltrack/plugins/`, `dltrack/scripts/`,
`dltrack/_cli.py`) live one directory up, alongside the client package's -- one git repo, one
source tree, two distributions built from different subsets of it (see this package's
`pyproject.toml`). `force-include` is the only mechanism that can pull files in from outside this
project's own directory, but it bypasses `[tool.hatch.build] exclude` entirely, so filtering
`tests`/`_tests`/`__pycache__` out has to happen here instead, by building the `force_include`
mapping ourselves instead of declaring it statically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_SOURCE_DIRS = ("serve", "plugins", "scripts")
_SOURCE_FILES = ("_cli.py",)
_EXCLUDED_DIR_NAMES = frozenset({"tests", "_tests", "__pycache__"})


class ServerSourcesBuildHook(BuildHookInterface[Any]):
    """Populate `force_include` with every server source file, skipping test directories."""

    def initialize(self, _version: str, build_data: dict[str, Any]) -> None:
        """Walk `../dltrack`'s server-owned subset, mapping each file into this wheel's `dltrack/`."""
        dltrack_root = Path(self.root).parent / "dltrack"
        force_include: dict[str, str] = build_data.setdefault("force_include", {})

        for name in _SOURCE_FILES:
            force_include[str(dltrack_root / name)] = f"dltrack/{name}"

        for name in _SOURCE_DIRS:
            for path in (dltrack_root / name).rglob("*"):
                if path.is_dir():
                    continue
                if _EXCLUDED_DIR_NAMES & set(path.relative_to(dltrack_root).parts):
                    continue
                force_include[str(path)] = f"dltrack/{path.relative_to(dltrack_root)}"
