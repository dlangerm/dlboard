"""The client's warnings, pointed at the line of the training script that made the call they are about."""

from __future__ import annotations

import inspect
import warnings
from typing import Final

_CALLED_THROUGH: Final = (
    "dlboard.client._",
    "dlboard.client.run",
    "dlboard.client.dlboard_logger",
    "pytorch_lightning.",
    "lightning_fabric.",
    "lightning_utilities.",
)
"""The modules a logging call passes through on its way to a warning: the client's own, and Lightning's."""


def warn(message: str) -> None:
    """
    Warn at the line that made the logging call: the first frame outside `_CALLED_THROUGH`.

    A fixed `stacklevel` would be right for one path only, and a `Run` is reached both directly and through
    `DLBoardLogger` (itself behind Lightning's `rank_zero_only`, or called by a `Trainer`).
    """
    frame, level = inspect.currentframe(), 1
    while frame is not None and str(frame.f_globals.get("__name__")).startswith(_CALLED_THROUGH):
        frame, level = frame.f_back, level + 1
    warnings.warn(message, UserWarning, stacklevel=level)
