"""What a pin looks like: a pin drawn in its colour, never named. No Dash callbacks."""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

from dash import html
from pydantic import BaseModel

from dlboard.models import PinColor
from dlboard.serve import Icon, icon

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dlboard.models import NamedPin


def pin_css_color(color: PinColor) -> str:
    """The CSS colour a pin of `color` is drawn in: a Mantine theme colour, so it follows the color scheme."""
    return "var(--mantine-color-text)" if color is PinColor.PLAIN else f"var(--mantine-color-{color.value}-6)"


def pin_glyph(color: PinColor) -> html.Span:
    """A pin in `color`: how a colour is shown wherever one is offered or named, in place of its name."""
    return html.Span(icon(Icon.PIN), style={"color": pin_css_color(color), "display": "inline-flex"})


class PinCell(BaseModel, frozen=True, extra="forbid"):
    """One colour a run is pinned in, as a grid cell draws it (`DlboardPins` in `grid_cell_components.js`)."""

    color: PinColor
    css: str
    """The colour to draw it in."""
    who: list[str]
    """Who pinned the run in it, by username."""
    mine: bool
    """Whether the viewer is one of them: the colours the cell's editor shows as theirs to take back."""


def pin_cells(pins: Iterable[NamedPin], me: int) -> list[PinCell]:
    """The colours one run is pinned in (a pin of a colour from each of several people is one cell), in `PinColor` order."""
    held = sorted(
        pins, key=lambda named: (list(PinColor).index(named.pin.color), named.pin.pinned_at, named.pin.id)
    )
    return [
        PinCell(
            color=color,
            css=pin_css_color(color),
            who=[named.username for named in same_color],
            mine=any(named.pin.pinned_by == me for named in same_color),
        )
        for color, group in itertools.groupby(held, key=lambda named: named.pin.color)
        for same_color in [list(group)]
    ]
