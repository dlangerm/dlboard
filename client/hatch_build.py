"""
Hatchling build hook: pull the client's code out of the shared `../dltrack` source tree.

The mirror image of `server/hatch_build.py`: that one force-includes the server's subset of
`../dltrack`; this one force-includes everything else -- the actual `dltrack` top-level package,
`client/`, `models/`, and the loose top-level modules (`_wire.py`, `_identity.py`, ...) -- skipping
the server-owned top-level entries and every test directory. See `server/hatch_build.py` for why a
hook does this instead of `[tool.hatch.build] include`/`exclude`: `exclude` doesn't apply to
`force-include`, and plain `include` can't reach a source directory outside this project's own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_SERVER_OWNED_TOP_LEVEL = frozenset({"serve", "plugins", "scripts", "_cli.py", "conftest.py", "tests"})
_EXCLUDED_DIR_NAMES = frozenset({"tests", "_tests", "__pycache__"})


class ClientSourcesBuildHook(BuildHookInterface[Any]):
    """Populate `force_include` with every client source file, skipping the server's and tests."""

    def initialize(self, _version: str, build_data: dict[str, Any]) -> None:
        """Walk `../dltrack`, mapping each client-owned file into this wheel's own `dltrack/`."""
        dltrack_root = Path(self.root).parent / "dltrack"
        force_include: dict[str, str] = build_data.setdefault("force_include", {})

        for path in dltrack_root.rglob("*"):
            if path.is_dir():
                continue
            relative = path.relative_to(dltrack_root)
            if relative.parts[0] in _SERVER_OWNED_TOP_LEVEL:
                continue
            if _EXCLUDED_DIR_NAMES & set(relative.parts):
                continue
            force_include[str(path)] = f"dltrack/{relative}"
