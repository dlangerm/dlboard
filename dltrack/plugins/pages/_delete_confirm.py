"""
Shared "delete with confirmation" button + modal.

Reused wherever a soft-deletable entity needs a delete affordance (project, experiment). Mirrors
`_description_editor.py`'s button-opens-modal shape.
"""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING

import dash_mantine_components as dmc
from dash import Input, Output, State
from dash.exceptions import PreventUpdate

from dltrack.models import constants

if TYPE_CHECKING:
    from collections.abc import Callable

    from dash import Dash
    from dash.development.base_component import Component


class DeleteConfirmIds(typing.NamedTuple):
    """Component ids for one delete-with-confirmation control."""

    button: str
    modal: str
    confirm: str
    cancel: str


def render_delete_control(ids: DeleteConfirmIds, *, label: str, entity_noun: str) -> list[Component]:
    """A delete button plus its (initially closed) confirmation modal."""
    return [
        dmc.Button(label, id=ids.button, n_clicks=0, color="red", variant="light", size="xs"),
        dmc.Modal(
            id=ids.modal,
            title=f"Delete this {entity_noun}?",
            opened=False,
            children=[
                dmc.Text(
                    f"This soft-deletes the {entity_noun} and everything under it. It can be "
                    "restored from the admin Trash until it's permanently purged."
                ),
                dmc.Group(
                    [
                        dmc.Button("Cancel", id=ids.cancel, variant="default"),
                        dmc.Button("Delete", id=ids.confirm, color="red"),
                    ],
                    justify="flex-end",
                    mt="sm",
                ),
            ],
        ),
    ]


def register_delete_callbacks(
    app: Dash,
    ids: DeleteConfirmIds,
    entity_id_state: State,
    *,
    on_confirm: Callable[[int], str],
) -> None:
    """
    Wire up open/confirm/cancel for one delete control that navigates away after deleting.

    `on_confirm` performs the actual soft-delete given the resolved entity id and returns the href
    to navigate to afterwards (e.g. back to the project list after deleting a project, or back to
    the owning project after deleting an experiment) -- a single callable rather than a separate
    delete/redirect pair, since building the redirect target (e.g. an experiment's `project_id`)
    often needs data only available *before* the delete happens.

    Navigation is a hard reload (`Location.refresh=True`), not a soft `use_pages` navigation --
    matches `simple_experiment_page.py`'s run-delete flow, and reliably lands on the target page
    instead of depending on `dcc.Location`'s client-side pathname routing picking up a bare `href`
    update.
    """

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(ids.modal, "opened", allow_duplicate=True),
        Input(ids.button, "n_clicks"),
        prevent_initial_call=True,
    )
    def open_modal(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return True

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(ids.modal, "opened", allow_duplicate=True),
        Input(ids.cancel, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_delete(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.LOCATION_ID, "href", allow_duplicate=True),
        Output(constants.LOCATION_ID, "refresh", allow_duplicate=True),
        Output(ids.modal, "opened", allow_duplicate=True),
        Input(ids.confirm, "n_clicks"),
        entity_id_state,
        prevent_initial_call=True,
    )
    def confirm_delete(n_clicks: int, entity_id: int | None) -> tuple[str, bool, bool]:
        if not n_clicks or entity_id is None:
            raise PreventUpdate
        return on_confirm(int(entity_id)), True, False
