"""The traces of a spread drawn as bands: the extremes, the inner band and a median line over them."""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def nested_band(  # noqa: PLR0913 -- the five lines of a spread, then how they are drawn
    x: Sequence[float],
    *,
    highs: Sequence[float],
    near_highs: Sequence[float],
    medians: Sequence[float],
    near_lows: Sequence[float],
    lows: Sequence[float],
    outer_fill: str,
    inner_fill: str,
    median_line: Mapping[str, object],
    shared: Mapping[str, object] = {},
    median_extra: Mapping[str, object] = {},
) -> list[dict[str, typing.Any]]:
    """
    Five Plotly scatter traces over `x`: the extremes and the inner band, each a fill between two lines, and the median.

    Each `tonexty` fill reaches back to the trace before it, so the order is what nests the bands. `shared` is set on
    every trace (an axis, a legend group) and `median_extra` on the median alone (its name, its hover).
    """
    hidden: dict[str, typing.Any] = {
        "type": "scatter",
        "mode": "lines",
        "x": list(x),
        "line": {"width": 0},
        "hoverinfo": "skip",
        **shared,
    }
    return [
        hidden | {"y": list(highs)},
        hidden | {"y": list(lows), "fill": "tonexty", "fillcolor": outer_fill},
        hidden | {"y": list(near_highs)},
        hidden | {"y": list(near_lows), "fill": "tonexty", "fillcolor": inner_fill},
        {"type": "scatter", "mode": "lines", "x": list(x), "y": list(medians), "line": dict(median_line)}
        | dict(shared)
        | dict(median_extra),
    ]
