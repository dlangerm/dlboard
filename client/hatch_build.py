"""
Hatchling build hook: pull the client's code out of the shared `../dlboard` source tree.

The mirror image of `server/hatch_build.py`: that one force-includes the server's subset of
`../dlboard`; this one force-includes everything else -- the actual `dlboard` top-level package,
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


def _dlboard_root(root: Path) -> Path:
    """
    Where this build's copy of the shared `dlboard/` tree actually lives.

    Building straight from the repo, it's a sibling of this project directory (`../dlboard`). An
    sdist is built by this exact same hook (see `[tool.hatch.build.targets.sdist.hooks.custom]` in
    `pyproject.toml`), which already wrote every force-included file under `dlboard/` *inside* the
    sdist -- so a wheel built from an extracted sdist finds its copy nested one level down instead,
    not up (verified by actually building a wheel from an extracted sdist, not just inspecting config).
    """
    sibling = root.parent / "dlboard"
    return sibling if sibling.is_dir() else root / "dlboard"


class ClientSourcesBuildHook(BuildHookInterface[Any]):
    """Populate `force_include` with every client source file, skipping the server's and tests."""

    def initialize(self, _version: str, build_data: dict[str, Any]) -> None:
        """Walk the shared `dlboard/` tree, mapping each client-owned file into this wheel's own `dlboard/`."""
        dlboard_root = _dlboard_root(Path(self.root))
        force_include: dict[str, str] = build_data.setdefault("force_include", {})

        for path in dlboard_root.rglob("*"):
            if path.is_dir():
                continue
            relative = path.relative_to(dlboard_root)
            if relative.parts[0] in _SERVER_OWNED_TOP_LEVEL:
                continue
            if _EXCLUDED_DIR_NAMES & set(relative.parts):
                continue
            force_include[str(path)] = f"dlboard/{relative}"
