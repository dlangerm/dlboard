"""The pin chips above a run list: show only the runs pinned in the colours picked, so a pinned run is never lost in a long list."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import dash_mantine_components as dmc
from dash import Input, Output, State

from dlboard.models import PinColor
from dlboard.serve import ClientsideScript
from dlboard.serve._component_ids import ValueId
from dlboard.serve._pages._pins._pin_view import pin_glyph

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dash import Dash
    from dash.development.base_component import Component

    from dlboard.serve._pages._run_list import RunListSurface
    from dlboard.serve._pages._run_list._ids import RunList

_APPLY_PIN_FILTER_JS = ClientsideScript(Path(__file__).with_name("apply_pin_filter.js"))


def pin_filter_id(surface: RunListSurface) -> ValueId[RunList]:
    return ValueId(f"run-list-pin-filter-{surface}")


def pin_filter(
    surface: RunListSurface, present: Iterable[PinColor], value: Iterable[str] = ()
) -> Component | None:
    """
    A chip for each colour some run of the list is pinned in, to show only the runs pinned in the ones picked.

    `None` when no run is pinned, so there is nothing to filter by.
    """
    colors = [color for color in PinColor if color in set(present)]
    if not colors:
        return None
    return dmc.ChipGroup(
        dmc.Group(
            [
                dmc.Chip(
                    pin_glyph(color),
                    value=color.value,
                    size="xs",
                    variant="outline",
                    **cast("dict[str, Any]", {"aria-label": f"Only {color.value} pins"}),
                )
                for color in colors
            ],
            gap=4,
        ),
        id=pin_filter_id(surface),
        multiple=True,
        value=list(value),
    )


def register_pin_filter(app: Dash, surface: RunListSurface, grid_id: str) -> None:
    """Filter the surface's grid, `grid_id`, by the chips picked."""
    app.clientside_callback(
        _APPLY_PIN_FILTER_JS.source,
        Output(pin_filter_id(surface), "style"),
        Input(pin_filter_id(surface), "value", allow_optional=True),
        State(grid_id, "id", allow_optional=True),
        prevent_initial_call=True,
    )
