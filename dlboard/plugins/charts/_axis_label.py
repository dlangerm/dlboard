"""Axis titles that fit inside their chart."""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import Field

DEFAULT_MAX_AXIS_LABEL_CHARS: Final = 36
"""
Roughly what fits along a default-width chart's x-axis title. A longer one is drawn past the chart's own area,
over whatever is next to it.
"""

VALUE_AXIS_WIDTH: Final = 56
"""
Width in px of a chart's value (y) axis: room for its tick labels, which the chart's title names.
Recharts' default is narrower than a long tick (`-1.2e-5`), so it was drawn over the plot.
"""

MIN_MAX_AXIS_LABEL_CHARS: Final = 12
"""Room for a bar chart's `median(...)` wrapper around at least a few characters of the name."""

# Its help text is an attribute docstring on each settings class that uses this alias: pydantic reads docstrings
# from class bodies, not from an alias.
MaxAxisLabelChars = Annotated[
    int,
    Field(ge=MIN_MAX_AXIS_LABEL_CHARS),
]
"""A chart setting: how long an axis title may be before it is shortened by `fit_axis_label`."""


def fit_axis_label(label: str, limit: int) -> str:
    """
    `label`, or its last `limit` characters behind an ellipsis when it is longer.

    The end of a metric name is the distinctive part; the start is usually the name of the panel it
    sits in (the default grouping splits a key on its first segment).
    """
    return label if len(label) <= limit else f"…{label[-(limit - 1) :]}"
