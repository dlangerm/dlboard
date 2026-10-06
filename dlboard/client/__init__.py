"""
Client-side objects in dlboard for use with training.

No re-exports here on purpose: `dlboard_logger.py` needs `pytorch_lightning` (the `[lightning]`
extra) at import time, to subclass its `Logger`. Re-exporting `DLBoardLogger` from this package's
own `__init__.py` would mean every client install needs lightning just to reach anything else
under `dlboard.client` (or `dlboard.client.artifacts`) -- importing any submodule always runs its
parent's `__init__.py` first. Import it from its own module instead:
`from dlboard.client.dlboard_logger import DLBoardLogger`.
"""
