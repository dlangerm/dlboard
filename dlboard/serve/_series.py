"""
The categorical palette charts draw series (usually runs) in: a core contract any chart plugin uses.

`series_color(i)` is a CSS variable, `--dl-series-<slot>`, not a hex value: the palette itself lives
in `_shell.css` -- one set of 8 values per color scheme, validated for colorblind separation and
contrast against the theme's surfaces -- and a theme plugin may override those variables in its own
stylesheet. So a chart never needs to know the theme, and every chart (and the run list's swatches)
colors run 7 the same way.

Eight colors run out: runs 3 and 11 share one. `series_dash(i)` tells them apart by how a line is drawn, so
a run keeps one color and one dash on every page it is drawn on, whatever else is drawn beside it.
"""

from __future__ import annotations

from enum import StrEnum

SERIES_SLOTS = 8
"""How many distinct colors the palette has; indexes past it wrap around."""


class SeriesDash(StrEnum):
    """How a series' line is drawn: what tells apart two series `series_color` gives one color."""

    SOLID = "solid"
    DASH = "dash"
    DOT = "dot"
    DASH_DOT = "dash-dot"

    @property
    def lengths(self) -> tuple[int, ...]:
        """The pattern in px: drawn, then skipped, and so on. Empty for a solid line."""
        match self:
            case SeriesDash.SOLID:
                return ()
            case SeriesDash.DASH:
                return (8, 4)
            case SeriesDash.DOT:
                return (2, 3)
            case SeriesDash.DASH_DOT:
                return (8, 3, 2, 3)

    @property
    def dasharray(self) -> str:
        """As SVG's `stroke-dasharray`, which Recharts' `strokeDasharray` (a dmc chart series') takes."""
        return " ".join(map(str, self.lengths)) or "none"

    @property
    def plotly(self) -> str:
        """As Plotly's `line.dash`."""
        return ",".join(f"{length}px" for length in self.lengths) or "solid"


def series_color(index: int) -> str:
    """
    The CSS color for series `index`.

    Pass a run id to color by run: it's stable across every chart that run appears in, and runs
    created one after another (a sweep) get distinct colors rather than colliding.
    """
    return f"var(--dl-series-{index % SERIES_SLOTS + 1})"


def series_dash(index: int) -> SeriesDash:
    """
    How series `index` is dashed: solid for the first `SERIES_SLOTS`, then dashed, dotted and dash-dotted.

    Pass a run id, as to `series_color`: two runs that share a color are `SERIES_SLOTS` apart, so they differ in this,
    unless they are `SERIES_SLOTS * len(SeriesDash)` apart.
    """
    dashes = list(SeriesDash)
    return dashes[index // SERIES_SLOTS % len(dashes)]


def series_swatch_class(index: int) -> str:
    """
    A class that draws a short line in `series_color(index)`, dashed as `series_dash(index)`, before an element's content.

    For labeling a run with how it is drawn where a chart legend isn't -- the run table, image captions --
    and usable anywhere a class name is (an ag-grid `cellClass`, not just a Dash component).
    """
    return f"dl-swatch dl-swatch-{index % SERIES_SLOTS + 1} dl-swatch-{series_dash(index)}"
