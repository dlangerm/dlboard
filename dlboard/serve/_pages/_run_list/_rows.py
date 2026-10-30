"""
Changing what a run list shows about its runs in place: a run drawn or hidden, pinned or unpinned, without drawing the list again.

A list is drawn again only when its runs, their hyperparameters or its columns change. Drawing a run, or pinning one, changes a
value in a row, which is patched in the grid (`apply_row_updates.js`), so the list keeps its scroll, sort, selection and filters.
The grid's filters are run again after the patch, since a row that no longer matches one must leave the view.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from dash import Input, Output
from dash.dcc import Store
from pydantic import BaseModel

from dlboard.serve import ClientsideScript
from dlboard.serve._component_ids import StoreId

if TYPE_CHECKING:
    from dash import Dash
    from dash.development.base_component import Component

    from dlboard.serve._pages._run_list._ids import RunList, RunListSurface

_APPLY_JS = ClientsideScript(Path(__file__).with_name("apply_row_updates.js"))


def row_updates_id(surface: RunListSurface) -> StoreId[RunList]:
    """Which rows changed, and their new values (`RowUpdates`), for the script to patch in the grid."""
    return StoreId(f"run-list-row-updates-{surface}")


class RowUpdate(BaseModel, frozen=True, extra="forbid"):
    """A run's row, and the fields of it that changed: each key a row field, each value what it is now."""

    run_id: int
    values: dict[str, Any]


class RowUpdates(BaseModel, frozen=True, extra="forbid"):
    """The rows of one grid to change in place."""

    grid: str
    rows: list[RowUpdate]

    def dump(self) -> dict[str, Any]:
        """As the store holds them."""
        return self.model_dump(mode="json")


def row_update_shells(surface: RunListSurface) -> list[Component]:
    """The store a list's row changes are written to: in the page's layout once, so a callback listening to it is not drawn with the list."""
    return [Store(id=row_updates_id(surface))]


def register_row_updates(app: Dash, surface: RunListSurface) -> None:
    """Patch the rows of the surface's grid with the changes written to its row-update store."""
    app.clientside_callback(
        _APPLY_JS.source,
        Output(row_updates_id(surface), "data", allow_duplicate=True),
        Input(row_updates_id(surface), "data"),
        prevent_initial_call=True,
    )
