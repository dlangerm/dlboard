"""
How tightly the app is laid out: compact unless a viewer chooses comfortable, in the account menu.

The choice is the browser's, like the color scheme: kept in `localStorage` (`density_store`), stamped onto `<html>` as
`data-dl-density` before the page paints (`preload.js`), and read by `_shell.css`, which tightens Mantine's spacing
scale for a compact page. Every `p="md"` and `gap="lg"` follows it, so a view needs no code of its own for it.
"""

from __future__ import annotations

import typing
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import dash_mantine_components as dmc
from dash import Input, Output
from dash.dcc import Store

from dlboard.serve._clientside_script import ClientsideScript

if TYPE_CHECKING:
    from dash import Dash
    from dash.development.base_component import Component


class Density(StrEnum):
    """How much room the app's padding and gaps take."""

    COMPACT = "compact"
    COMFORTABLE = "comfortable"


DEFAULT_DENSITY: typing.Final = Density.COMPACT
DENSITY_STORE_ID: typing.Final = "dl-density"
"""The `localStorage` key the choice is kept under, as JSON (`dcc.Store`'s own format), which `preload.js` reads."""
_DENSITY_CONTROL_ID: typing.Final = "dl-density-control"
_DENSITY_JS = ClientsideScript(Path(__file__).with_name("density.js"))


def density_store() -> Store:
    """The browser's choice, for the app shell."""
    return Store(id=DENSITY_STORE_ID, data=DEFAULT_DENSITY, storage_type="local")


def density_control() -> list[Component]:
    """The account menu's Comfortable/Compact switch, under its label."""
    return [
        dmc.MenuLabel("Density"),
        dmc.SegmentedControl(
            id=_DENSITY_CONTROL_ID,
            data=[{"value": density, "label": density.capitalize()} for density in Density],
            value=DEFAULT_DENSITY,
            size="xs",
            fullWidth=True,
            mx="sm",
            mb=6,
        ),
    ]


def install_density(app: Dash) -> None:
    """Keep the control and the stored choice as one, and lay the page out again when the control changes."""
    app.clientside_callback(
        _DENSITY_JS.source,
        Output(DENSITY_STORE_ID, "data"),
        Output(_DENSITY_CONTROL_ID, "value"),
        Input(DENSITY_STORE_ID, "data"),
        Input(_DENSITY_CONTROL_ID, "value"),
    )
