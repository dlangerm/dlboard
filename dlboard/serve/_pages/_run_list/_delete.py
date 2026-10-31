"""Deleting the runs selected in a run list: asked once, saying how many, and done when confirmed."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import dash_mantine_components as dmc
from dash import Input, Output, State, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate

from dlboard.serve import ClientsideScript, get_current_user, get_data_store
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import ButtonId, DivId, ModalId, StoreId

if TYPE_CHECKING:
    from dash import Dash
    from dash.development.base_component import Component

    from dlboard.serve._pages._run_list._ids import RunList, RunListSurface


def _modal(surface: RunListSurface) -> ModalId[RunList]:
    return ModalId(f"run-list-delete-modal-{surface}")


def _body(surface: RunListSurface) -> DivId[RunList]:
    return DivId(f"run-list-delete-body-{surface}")


def _pending(surface: RunListSurface) -> StoreId[RunList]:
    return StoreId(f"run-list-delete-pending-{surface}")


def _confirm(surface: RunListSurface) -> ButtonId[RunList]:
    return ButtonId(f"run-list-delete-confirm-{surface}")


def _cancel(surface: RunListSurface) -> ButtonId[RunList]:
    return ButtonId(f"run-list-delete-cancel-{surface}")


def _confirmed(surface: RunListSurface) -> StoreId[RunList]:
    return StoreId(f"run-list-delete-confirmed-{surface}")


def _reload(surface: RunListSurface) -> StoreId[RunList]:
    return StoreId(f"run-list-delete-reload-{surface}")


def delete_shells(surface: RunListSurface) -> list[Component]:
    """The modal that asks before runs are deleted, and the store that holds which: in the page's layout once."""
    return [
        Store(id=_pending(surface)),
        Store(id=_confirmed(surface)),
        Store(id=_reload(surface)),
        dmc.Modal(
            id=_modal(surface),
            title="Delete these runs?",
            opened=False,
            children=[
                dmc.Text(id=_body(surface)),
                dmc.Group(
                    [
                        dmc.Button("Cancel", id=_cancel(surface), variant="default"),
                        dmc.Button("Delete", id=_confirm(surface), color="red"),
                    ],
                    justify="flex-end",
                    mt="sm",
                ),
            ],
        ),
    ]


_CLICK_GATE_JS = ClientsideScript(Path(__file__).with_name("click_gate.js"))
_RELOAD_PAGE_JS = ClientsideScript(Path(__file__).with_name("reload_page.js"))


class Asked(NamedTuple):
    """The modal opened on some runs: what to write to `delete_outputs`, in order."""

    opened: bool
    body: str
    pending: list[int]


def ask_to_delete(run_ids: frozenset[int]) -> Asked:
    """Ask before deleting `run_ids`, saying how many."""
    count = len(run_ids)
    these, they, them = ("this run", "It", "it") if count == 1 else (f"these {count} runs", "They", "them")
    return Asked(
        opened=True,
        body=(
            f"This soft-deletes {these} and everything under {them}. "
            f"{they} can be restored from the admin Trash until permanently purged."
        ),
        pending=sorted(run_ids),
    )


def delete_outputs(surface: RunListSurface) -> list[Output]:
    """The `Output`s `Asked` is written to, in its order, for the callback that answers a request to delete."""
    return [
        Output(_modal(surface), "opened", allow_duplicate=True),
        Output(_body(surface), "children"),
        Output(_pending(surface), "data"),
    ]


def register_delete(app: Dash, surface: RunListSurface, *, notify: str) -> None:
    """
    Cancel the modal, and delete the runs it was opened on when it is confirmed, then draw the page again.

    The runs are gone from whatever the page drew, so it is loaded afresh. `notify` is the page's notification container,
    which says why a delete was refused. The confirmation reaches the server through a store (`click_gate.js`), not the
    button: a callback that writes the app shell's `location` and listens to a button the page draws is called as the page is.
    """

    @app.callback(
        Output(_modal(surface), "opened", allow_duplicate=True),
        Input(_cancel(surface), "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_delete(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False

    app.clientside_callback(
        _CLICK_GATE_JS.source,
        Output(_confirmed(surface), "data"),
        Input(_confirm(surface), "n_clicks"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output(_modal(surface), "opened", allow_duplicate=True),
        Output(_reload(surface), "data"),
        Output(notify, "sendNotifications", allow_duplicate=True),
        Input(_confirmed(surface), "data"),
        State(_pending(surface), "data"),
        prevent_initial_call=True,
    )
    def confirm_delete(confirmed: int | None, run_ids: list[int] | None) -> tuple[bool, Any, Any]:
        if not confirmed or not run_ids:
            raise PreventUpdate
        try:
            get_data_store().delete_runs(frozenset(run_ids), get_current_user())
        except PermissionError:
            notice = {
                "action": "show",
                "id": "run-list-not-yours-to-delete",
                "title": "Not yours to delete",
                "message": "Deleting runs takes editing the project.",
                "color": "red",
                "autoClose": 6_000,
            }
            return False, no_update, [notice]
        return False, confirmed, no_update

    app.clientside_callback(
        _RELOAD_PAGE_JS.source,
        Output(constants.LOCATION_ID, "href", allow_duplicate=True),  # always there, never written
        Input(_reload(surface), "data"),
        prevent_initial_call=True,
    )
