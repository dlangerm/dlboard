"""
Client-side objects in dltrack for use with training.

No re-exports here on purpose: `dltrack_logger.py` needs `pytorch_lightning` (the `[lightning]`
extra) at import time, to subclass its `Logger`. Re-exporting `DLTrackLogger` from this package's
own `__init__.py` would mean every client install needs lightning just to reach anything else
under `dltrack.client` (or `dltrack.client.artifacts`) -- importing any submodule always runs its
parent's `__init__.py` first. Import it from its own module instead:
`from dltrack.client.dltrack_logger import DLTrackLogger`.
"""
