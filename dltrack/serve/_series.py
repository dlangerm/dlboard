"""
The categorical palette charts draw series (usually runs) in: a core contract any chart plugin uses.

`series_color(i)` is a CSS variable, `--dl-series-<slot>`, not a hex value: the palette itself lives
in `_shell.css` -- one set of 8 values per color scheme, validated for colorblind separation and
contrast against the theme's surfaces -- and a theme plugin may override those variables in its own
stylesheet. So a chart never needs to know the theme, and every chart (and the run list's swatches)
colors run 7 the same way.
"""

from __future__ import annotations

SERIES_SLOTS = 8
"""How many distinct colors the palette has; indexes past it wrap around."""


def series_color(index: int) -> str:
    """
    The CSS color for series `index`.

    Pass a run id to color by run: it's stable across every chart that run appears in, and runs
    created one after another (a sweep) get distinct colors rather than colliding.
    """
    return f"var(--dl-series-{index % SERIES_SLOTS + 1})"
