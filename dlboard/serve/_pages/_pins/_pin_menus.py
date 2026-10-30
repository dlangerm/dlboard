"""The Pin and Unpin menus of a run list's bar: the colours to pin the selected runs in, as pins in those colours."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import dash_mantine_components as dmc

from dlboard.models import PinColor
from dlboard.serve import Icon, icon
from dlboard.serve._pages._pins._pin_view import pin_glyph
from dlboard.serve._pages._run_list import RunAction, RunListSurface
from dlboard.serve._pages._run_list._ids import action_button

if TYPE_CHECKING:
    from dash.development.base_component import Component

_COLUMNS = 5
"""How many colours a row of the menu holds: ten colours are two rows of five."""


def _colors_menu(surface: RunListSurface, action: RunAction, label: str) -> Component:
    """A button that opens the colours to `action` the selected runs in: each a pin in its colour, named only to a screen reader."""
    buttons = action_button(surface)
    return dmc.Menu(
        [
            dmc.MenuTarget(
                dmc.Button(
                    label,
                    variant="light",
                    color="gray",
                    size="compact-xs",
                    leftSection=icon(Icon.PIN),
                    rightSection=icon(Icon.CHEVRON_DOWN),
                )
            ),
            dmc.MenuDropdown(
                dmc.SimpleGrid(
                    [
                        dmc.MenuItem(
                            pin_glyph(color),
                            id=buttons.of(action, color=color),
                            p=6,
                            **cast("dict[str, Any]", {"aria-label": f"{label} {color.value}"}),
                        )
                        for color in PinColor
                    ],
                    cols=_COLUMNS,
                    spacing=2,
                ),
            ),
        ],
        position="bottom-start",
        withinPortal=True,
    )


def pin_menus(surface: RunListSurface) -> list[Component]:
    """The Pin and Unpin menus for the bar of `surface`'s list."""
    return [
        _colors_menu(surface, RunAction.PIN, "Pin"),
        _colors_menu(surface, RunAction.UNPIN, "Unpin"),
    ]
