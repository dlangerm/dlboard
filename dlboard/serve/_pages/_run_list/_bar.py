"""The bar of actions on the rows selected in a run list, and the browser half of every request to the list."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import dash_mantine_components as dmc
from dash import Input, Output, State, html

from dlboard.serve import ClientsideScript, Icon, icon
from dlboard.serve._pages._run_list._ids import (
    RunListSurface,
    action_button,
    bar_id,
    count_id,
    request_store_id,
)

if TYPE_CHECKING:
    from dash import Dash
    from dash.development.base_component import Component

    from dlboard.serve._pages._run_list._model import RunAction

_ACTION_GATE_JS = ClientsideScript(Path(__file__).with_name("action_gate.js"))
_CELL_GATE_JS = ClientsideScript(Path(__file__).with_name("cell_gate.js"))
_SELECTION_BAR_JS = ClientsideScript(Path(__file__).with_name("selection_bar.js"))


def action_bar(surface: RunListSurface, *actions: Component) -> html.Div:
    """The `actions` on the selected rows, hidden until a row is selected, beside how many are."""
    return html.Div(
        dmc.Group(
            [dmc.Text(id=count_id(surface), size="xs", fw=600, c="dimmed"), *actions],
            gap=4,
            wrap="wrap",
        ),
        id=bar_id(surface),
        style={"display": "none"},
        className="dl-run-list-bar",
    )


def simple_action(
    surface: RunListSurface, action: RunAction, label: str, *, icon_name: Icon, color: str = "gray"
) -> Component:
    """A button that asks for `action` on the selected rows."""
    return dmc.Button(
        label,
        id=action_button(surface).of(action),
        n_clicks=0,
        variant="light",
        color=color,
        size="compact-xs",
        leftSection=icon(icon_name),
    )


def register_run_list(app: Dash, surface: RunListSurface, grid_id: str) -> None:
    """
    The browser's half of a run list on `surface`, drawn as the grid `grid_id`.

    The bar follows the selection, and what is clicked (a button of the bar, or a row's eye) is written to the surface's
    request store.

    The page's callback listens to that store, which is in its layout from the start, and not to the buttons and the grid, which
    mount with the list and would call it as they do.
    """
    app.clientside_callback(
        _SELECTION_BAR_JS.source,
        Output(bar_id(surface), "style"),
        Output(count_id(surface), "children"),
        Input(grid_id, "selectedRows", allow_optional=True),
    )
    app.clientside_callback(
        _ACTION_GATE_JS.source,
        Output(request_store_id(surface), "data"),
        Input(action_button(surface).all(), "n_clicks", allow_optional=True),
        State(grid_id, "selectedRows", allow_optional=True),
        prevent_initial_call=True,
    )
    app.clientside_callback(
        _CELL_GATE_JS.source,
        Output(request_store_id(surface), "data", allow_duplicate=True),
        Input(grid_id, "cellRendererData", allow_optional=True),
        prevent_initial_call=True,
    )
