"""
The jump palette: Ctrl/Cmd+K (or "/") from anywhere, type a few letters, Enter to open that page.

Costs nothing until used. Opening it is purely client-side (`jump_shortcut.js`), and the list of
projects and experiments is only fetched from the server when it opens, so no page load pays for
it. Picking a result navigates client-side, like any in-app link.
"""

from __future__ import annotations

import typing
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import dash_mantine_components as dmc
from dash import Input, Output
from dash.exceptions import PreventUpdate

from dlboard.serve._assets import AssetKind, serve_asset
from dlboard.serve._backend._data_store import get_data_store
from dlboard.serve._clientside_script import ClientsideScript
from dlboard.serve._icons import Icon, icon
from dlboard.serve._url import relative_path

if TYPE_CHECKING:
    from dash import Dash

JUMP_MODAL_ID: typing.Final = "jump-modal"
JUMP_SELECT_ID: typing.Final = "jump-select"

_SHORTCUT_JS = Path(__file__).with_name("jump_shortcut.js")
_NAVIGATE_JS = ClientsideScript(Path(__file__).with_name("jump_navigate.js"))


def jump_trigger() -> dmc.Button:
    """The header's "Search… Ctrl K" button; `jump_shortcut.js` finds it by its `data-` attribute."""
    return dmc.Button(
        "Search…",
        leftSection=icon(Icon.SEARCH),
        rightSection=dmc.Kbd("Ctrl K", size="xs"),
        variant="default",
        size="xs",
        c="dimmed",
        visibleFrom="sm",
        **cast("dict[str, Any]", {"data-dl-jump-modal": JUMP_MODAL_ID}),
    )


def jump_modal() -> dmc.Modal:
    """The palette itself: one searchable select, filled in when the modal opens."""
    return dmc.Modal(
        id=JUMP_MODAL_ID,
        opened=False,
        withCloseButton=False,
        size="lg",
        yOffset="12vh",
        padding="xs",
        children=dmc.Select(
            id=JUMP_SELECT_ID,
            data=[],
            value=None,
            searchable=True,
            clearable=False,
            placeholder="Jump to a project or experiment…",
            nothingFoundMessage="Nothing matches",
            leftSection=icon(Icon.SEARCH),
            maxDropdownHeight=360,
            limit=50,
            size="md",
            variant="unstyled",
            selectFirstOptionOnChange=True,
            **cast("dict[str, Any]", {"data-autofocus": True}),
        ),
    )


def _destinations() -> list[dict[str, Any]]:
    """Every project, then each project's experiments, as Select groups whose values are paths."""
    store = get_data_store()
    projects = list(store.get_projects())
    groups: list[dict[str, Any]] = [
        {
            "group": "Projects",
            "items": [{"value": relative_path(f"/project/{p.id}"), "label": p.name} for p in projects],
        }
    ]
    for project in projects:
        experiments = [
            {
                "value": relative_path(f"/experiment/{e.id}"),
                "label": e.name or f"Experiment {e.id}",
            }
            for e in store.get_experiments(project.id)
        ]
        if experiments:
            groups.append({"group": project.name, "items": experiments})
    return groups


def install_jump(app: Dash) -> None:
    """Serve the shortcut listener and wire the palette's data and navigation, on `app` only."""
    serve_asset(app, AssetKind.SCRIPT, _SHORTCUT_JS.name, _SHORTCUT_JS.read_bytes())

    @app.callback(
        Output(JUMP_SELECT_ID, "data"),
        Input(JUMP_MODAL_ID, "opened"),
        prevent_initial_call=True,
    )
    def load_destinations(opened: bool) -> list[dict[str, Any]]:  # noqa: FBT001
        if not opened:
            raise PreventUpdate
        return _destinations()

    app.clientside_callback(
        _NAVIGATE_JS.source,
        Output(JUMP_MODAL_ID, "opened"),
        Output(JUMP_SELECT_ID, "value"),
        Input(JUMP_SELECT_ID, "value"),
        prevent_initial_call=True,
    )
