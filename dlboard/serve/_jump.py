"""
The jump palette: Ctrl/Cmd+K (or "/") from anywhere, type a few letters, Enter to open that page.

Costs nothing until used. Opening it is purely client-side (`jump_shortcut.js`), and the projects, experiments and
comparisons are only fetched from the server when it opens (the newest of them) and as someone types (a search of every
project), so no page load pays for it. Picking a result navigates client-side, like any in-app link.
"""

from __future__ import annotations

import typing
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import dash_mantine_components as dmc
from dash import Input, Output, State
from dash.exceptions import PreventUpdate

from dlboard.serve._assets import AssetKind, serve_asset
from dlboard.serve._backend._auth import get_current_user
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
_SELECT_FIRST_JS = ClientsideScript(Path(__file__).with_name("jump_select_first.js"))


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
            placeholder="Jump to a project, experiment or comparison…",
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


JUMP_LIMIT: typing.Final = 25
"""
How many experiments, and how many comparisons, the palette lists at once: the newest, or those a search finds (#590).

A project's experiments are unbounded, so the palette never lists every one of every project: each is a search away.
"""


def _destinations(search: str) -> list[dict[str, Any]]:
    """
    The projects, experiments and comparisons the viewer may see that `search` finds, as Select options whose values are paths.

    The experiments and comparisons are each one read of every project the viewer sees, the newest `JUMP_LIMIT` that match.
    A flat list, each named with its project: Mantine's `limit` counts a group however few of its options match, so a Select of
    a group for each project could never reach past its first `limit` projects.
    """
    store = get_data_store()
    projects = {p.id: p for p in store.get_projects()}
    found = search.casefold()
    options = [
        {"value": relative_path(f"/project/{p.id}"), "label": p.name}
        for p in projects.values()
        if found in p.name.casefold()
    ]
    options.extend(
        {
            "value": relative_path(f"/experiment/{e.id}"),
            "label": f"{e.name or f'Experiment {e.id}'} · {projects[e.project_id].name}",
        }
        for e in store.search_experiments_in(list(projects), name_contains=search, limit=JUMP_LIMIT)
    )
    options.extend(
        {
            "value": relative_path(f"/comparison/{c.id}"),
            "label": f"{c.name} (comparison) · {projects[c.project_id].name}",
        }
        for c in store.search_comparisons_in(
            list(projects), get_current_user().id, name_contains=search, limit=JUMP_LIMIT
        )
    )
    return options


def install_jump(app: Dash) -> None:
    """Serve the shortcut listener and wire the palette's data and navigation, on `app` only."""
    serve_asset(app, AssetKind.SCRIPT, _SHORTCUT_JS.name, _SHORTCUT_JS.read_bytes())

    @app.callback(
        Output(JUMP_SELECT_ID, "data"),
        Input(JUMP_MODAL_ID, "opened"),
        Input(JUMP_SELECT_ID, "searchValue"),
        prevent_initial_call=True,
    )
    def find_destinations(opened: bool, search: str | None) -> list[dict[str, Any]]:  # noqa: FBT001
        """The newest destinations when the palette opens, and those a search of every project finds as someone types."""
        if not opened:
            raise PreventUpdate
        return _destinations(search or "")

    app.clientside_callback(
        _SELECT_FIRST_JS.source,
        Input(JUMP_SELECT_ID, "data"),
        State(JUMP_SELECT_ID, "id"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        _NAVIGATE_JS.source,
        Output(JUMP_MODAL_ID, "opened"),
        Output(JUMP_SELECT_ID, "value"),
        Input(JUMP_SELECT_ID, "value"),
        prevent_initial_call=True,
    )
