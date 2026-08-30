"""
Wraps a `.js` file for use as a Dash clientside-callback body.

Keeping clientside callback JS in its own file (rather than as a Python string literal) lets it
be linted/formatted by Biome like every other `dltrack/**/*.js` file, and reused across callbacks.
"""

from functools import cached_property
from pathlib import Path


class ClientsideScript:
    """Lazily reads a `.js` file's contents for use as a `Dash.clientside_callback` body."""

    def __init__(self, path: Path) -> None:
        assert path.is_file(), f"clientside script not found: {path}"
        self._path = path

    @cached_property
    def source(self) -> str:
        return self._path.read_text()
