"""Shared editable name/description header, reused by the project and experiment pages."""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING

import dash_mantine_components as dmc
from dash import Input, Output, State
from dash.exceptions import PreventUpdate

from dltrack.serve import Icon
from dltrack.serve._pages._dash_helpers import tooltipped_action_icon

if TYPE_CHECKING:
    from collections.abc import Callable

    from dash import Dash
    from dash.development.base_component import Component


class DescriptionEditorIds(typing.NamedTuple):
    """Component ids for one editable name/description header."""

    header: str
    edit_button: str
    modal: str
    textarea: str
    save: str
    cancel: str


def render_header(
    ids: DescriptionEditorIds,
    *,
    title: str,
    description: str,
    extra_actions: list[Component] | None = None,
) -> Component:
    """
    Build the title/description display plus its (initially closed) edit modal.

    `extra_actions` are rendered inline in the same group as the title/edit-icon (e.g. a delete
    icon next to the edit icon) -- optional since most callers have no such action.

    Carries no margin of its own — callers decide spacing, since some (the experiment page) place
    this inline in a flex row alongside other controls, where an internal margin would misalign it
    against its row-siblings, while others (the project page) place it alone on the page.
    """
    return dmc.Stack(
        [
            dmc.Group(
                [
                    dmc.Text(title, size="sm", fw=600),
                    tooltipped_action_icon(
                        Icon.EDIT, component_id=ids.edit_button, label="Edit name/description"
                    ),
                    *(extra_actions or []),
                ],
                gap="xs",
            ),
            dmc.Text(description or "No description", c="dimmed", size="sm"),
            dmc.Modal(
                id=ids.modal,
                title="Edit description",
                opened=False,
                children=[
                    dmc.Textarea(id=ids.textarea, value=description, minRows=3, autosize=True),
                    dmc.Group(
                        [
                            dmc.Button("Cancel", id=ids.cancel, variant="default"),
                            dmc.Button("Save", id=ids.save),
                        ],
                        justify="flex-end",
                        mt="sm",
                    ),
                ],
            ),
        ],
        gap=2,
    )


def register_edit_callbacks(  # noqa: PLR0913
    app: Dash,
    ids: DescriptionEditorIds,
    entity_id_state: State,
    *,
    fetch: Callable[[int], tuple[str, str]],
    save: Callable[[int, str], tuple[str, str]],
    extra_actions: list[Component] | None = None,
) -> None:
    """
    Wire up open/save/cancel for one editable header.

    `fetch`/`save` take the entity id (read off `entity_id_state`) and return `(name, description)`.
    """

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(ids.modal, "opened", allow_duplicate=True),
        Output(ids.textarea, "value"),
        Input(ids.edit_button, "n_clicks"),
        entity_id_state,
        prevent_initial_call=True,
    )
    def open_edit_modal(n_clicks: int, entity_id: int) -> tuple[bool, str]:
        if not n_clicks:
            raise PreventUpdate
        _name, description = fetch(entity_id)
        return True, description

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(ids.modal, "opened", allow_duplicate=True),
        Output(ids.header, "children", allow_duplicate=True),
        Input(ids.save, "n_clicks"),
        State(ids.textarea, "value"),
        entity_id_state,
        prevent_initial_call=True,
    )
    def save_description(
        n_clicks: int, new_description: str | None, entity_id: int
    ) -> tuple[bool, Component]:
        if not n_clicks:
            raise PreventUpdate
        name, description = save(entity_id, new_description or "")
        return False, render_header(ids, title=name, description=description, extra_actions=extra_actions)

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(ids.modal, "opened", allow_duplicate=True),
        Input(ids.cancel, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_edit(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False
