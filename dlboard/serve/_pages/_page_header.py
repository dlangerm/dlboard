"""
The header of a project's, an experiment's or a comparison's page: its name and description, and what can be done with it.

A save writes the title and the description in place, never the header again: what sits beside them (a delete action, a
menu, the page's own controls) is drawn once, by the page's `layout()`, with whatever it needs from the request.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING

import dash_mantine_components as dmc
from dash import Input, Output, State, html
from dash.exceptions import PreventUpdate

from dlboard.serve import Icon
from dlboard.serve._component_ids import ButtonId, DivId, ModalId, ValueId
from dlboard.serve._pages._dash_helpers import tooltipped_action_icon

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from dash import Dash
    from dash.development.base_component import Component

NO_DESCRIPTION: typing.Final = "No description"


@dataclass(frozen=True)
class PageHeaderIds[P]:
    """The ids of one page's header; `name` is `None` where the name is not edited here."""

    title: DivId[P]
    description: DivId[P]
    edit_button: ButtonId[P]
    modal: ModalId[P]
    textarea: ValueId[P]
    save: ButtonId[P]
    cancel: ButtonId[P]
    name: ValueId[P] | None

    @classmethod
    def of(cls, prefix: str, *, name: bool = False) -> PageHeaderIds[P]:
        """The header ids of the page `prefix` names, with a name field if `name`."""
        return cls(
            title=DivId(f"{prefix}-title"),
            description=DivId(f"{prefix}-description"),
            edit_button=ButtonId(f"{prefix}-edit-open"),
            modal=ModalId(f"{prefix}-edit-modal"),
            textarea=ValueId(f"{prefix}-edit-description"),
            save=ButtonId(f"{prefix}-edit-save"),
            cancel=ButtonId(f"{prefix}-edit-cancel"),
            name=ValueId(f"{prefix}-edit-name") if name else None,
        )

    @property
    def edit_label(self) -> str:
        """What the edit icon and its modal say they change."""
        return "Edit name and description" if self.name is not None else "Edit description"


def page_header[P](  # noqa: PLR0913 -- the ids and text, then each slot a page can fill
    ids: PageHeaderIds[P],
    *,
    title: str,
    description: str,
    editable: bool = True,
    actions: Sequence[Component] = (),
    meta: Sequence[Component] = (),
    aside: Sequence[Component] = (),
) -> Component:
    """
    The title and description, with an edit icon if `editable` and its (closed) modal.

    `actions` sit beside the title (a delete icon), `meta` under the description (who owns it, a plugin's line), and `aside`
    at the header's right (the page's own controls). Carries no margin: the page places it.
    """
    heading = dmc.Stack(
        [
            dmc.Group(
                [
                    dmc.Title(title, order=3, id=ids.title),
                    *(
                        [
                            tooltipped_action_icon(
                                Icon.EDIT, component_id=ids.edit_button, label=ids.edit_label
                            )
                        ]
                        if editable
                        else []
                    ),
                    *actions,
                ],
                gap="xs",
            ),
            dmc.Text(description or NO_DESCRIPTION, c="dimmed", size="sm", id=ids.description),
            *meta,
        ],
        gap=2,
        style={"flex": 1, "minWidth": 0},
    )
    return html.Div(
        [
            dmc.Group([heading, *aside], justify="space-between", align="flex-start", wrap="nowrap"),
            *([_edit_modal(ids, title=title, description=description)] if editable else []),
        ]
    )


def _edit_modal[P](ids: PageHeaderIds[P], *, title: str, description: str) -> dmc.Modal:
    return dmc.Modal(
        id=ids.modal,
        title=ids.edit_label,
        opened=False,
        children=[
            *(
                [dmc.TextInput(id=ids.name, label="Name", value=title, mb="sm")]
                if ids.name is not None
                else []
            ),
            dmc.Textarea(id=ids.textarea, label="Description", value=description, minRows=3, autosize=True),
            dmc.Group(
                [
                    dmc.Button("Cancel", id=ids.cancel, variant="default"),
                    dmc.Button("Save", id=ids.save),
                ],
                justify="flex-end",
                mt="md",
            ),
        ],
    )


def register_page_header[P](
    app: Dash,
    ids: PageHeaderIds[P],
    entity: State,
    *,
    fetch: Callable[[int], tuple[str, str]],
    save: Callable[[int, str | None, str], tuple[str, str]],
) -> None:
    """
    Open, save and cancel the edit of one page's header.

    `fetch` takes the entity's id (read off `entity`) and returns its `(name, description)`, to open the modal on what is
    stored; `save` takes the id, the new name (`None` unless `ids.name`) and description, and returns what was saved.
    """
    name_value: list[Output] = [Output(ids.name, "value")] if ids.name is not None else []
    name_state: list[State] = [State(ids.name, "value")] if ids.name is not None else []

    @app.callback(
        Output(ids.modal, "opened", allow_duplicate=True),
        Output(ids.textarea, "value"),
        *name_value,
        Input(ids.edit_button, "n_clicks"),
        entity,
        prevent_initial_call=True,
    )
    def open_edit(n_clicks: int | None, entity_id: int) -> tuple[bool, str] | tuple[bool, str, str]:
        if not n_clicks:
            raise PreventUpdate
        name, description = fetch(entity_id)
        return (True, description, name) if name_value else (True, description)

    @app.callback(
        Output(ids.modal, "opened", allow_duplicate=True),
        Output(ids.title, "children"),
        Output(ids.description, "children"),
        Input(ids.save, "n_clicks"),
        State(ids.textarea, "value"),
        entity,
        *name_state,
        prevent_initial_call=True,
    )
    def save_edit(
        n_clicks: int | None, description: str | None, entity_id: int, name: str | None = None
    ) -> tuple[bool, str, str]:
        if not n_clicks:
            raise PreventUpdate
        saved_name, saved_description = save(entity_id, name, (description or "").strip())
        return False, saved_name, saved_description or NO_DESCRIPTION

    @app.callback(
        Output(ids.modal, "opened", allow_duplicate=True),
        Input(ids.cancel, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_edit(n_clicks: int | None) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False
