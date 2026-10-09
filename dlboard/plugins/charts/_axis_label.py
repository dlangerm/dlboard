"""Axis titles that fit inside their chart."""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import Field

DEFAULT_MAX_AXIS_LABEL_CHARS: Final = 36
"""
Roughly what fits along a default-height (300px) chart's rotated y-axis title -- the one that can
run out of room. A longer one is drawn past the chart's own area, over whatever is next to it.
"""

MIN_MAX_AXIS_LABEL_CHARS: Final = 12
"""Room for a bar chart's `median(...)` wrapper around at least a few characters of the name."""

MaxAxisLabelChars = Annotated[
    int,
    Field(
        ge=MIN_MAX_AXIS_LABEL_CHARS,
        description="Longest axis title, in characters. A longer metric name is shortened to its last "
        "characters behind an ellipsis, so it stays inside the chart; raise it for a taller or wider chart.",
    ),
]
"""A chart setting: how long an axis title may be before it is shortened by `fit_axis_label`."""


def fit_axis_label(label: str, limit: int) -> str:
    """
    `label`, or its last `limit` characters behind an ellipsis when it is longer.

    The end of a metric name is the distinctive part; the start is usually the name of the panel it
    sits in (the default grouping splits a key on its first segment).
    """
    return label if len(label) <= limit else f"…{label[-(limit - 1) :]}"
