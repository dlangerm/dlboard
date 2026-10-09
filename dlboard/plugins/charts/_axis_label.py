"""Axis titles that fit inside their chart."""

from __future__ import annotations

from typing import Final

MAX_AXIS_LABEL_CHARS: Final = 36
"""
Roughly what fits along a default-height (300px) chart's rotated y-axis title -- the one that can
run out of room. A longer one is drawn past the chart's own area, over whatever is next to it.
"""


def fit_axis_label(label: str, limit: int = MAX_AXIS_LABEL_CHARS) -> str:
    """
    `label`, or its last `limit` characters behind an ellipsis when it is longer.

    The end of a metric name is the distinctive part; the start is usually the name of the panel it
    sits in (the default grouping splits a key on its first segment).
    """
    return label if len(label) <= limit else f"…{label[-(limit - 1) :]}"
