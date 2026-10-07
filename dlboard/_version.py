"""
The single source of dlboard's version.

Both `client/pyproject.toml` and `server/pyproject.toml` read `__version__` from here via hatch's
dynamic `[tool.hatch.version]` (see their own `path` setting), instead of each hardcoding its own
`version` field -- which is how the two distributions drifted out of lockstep before this existed:
nothing kept `dlboard`'s version and `dlboard-server`'s `dlboard==...` pin pointed at the same
release. Bump this one file to cut a release; neither `pyproject.toml`'s `version` is ever edited
directly.

Also read at runtime -- `Identity.server_version` (see `dlboard._wire`) is what a client's `whoami`
handshake actually sees.
"""

# No type annotation -- hatchling's default `regex` version source (`[tool.hatch.version]` in both
# `pyproject.toml`s) matches `__version__ *= *"..."` literally and wouldn't see this past a `: str`.
__version__ = "0.1.2"
