"""
The comparison page's header: its name and description, whose it is, and what its owner and anyone else can do with it.

The pieces that change (the title, the description, the share toggle's label) are updated in place, never drawn again with
their buttons: Dash calls a callback listening to a component as it is drawn by another callback, with nothing clicked.
"""

from __future__ import annotations

import typing
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, html
from dash.exceptions import PreventUpdate

from dlboard.serve import ClientsideScript, Icon, get_current_user, get_data_store, icon
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import ButtonId, DivId, ModalId, ValueId
from dlboard.serve._pages._comparison import _ids as ids
from dlboard.serve._pages._dash_helpers import CallbackStates, tooltipped_action_icon
from dlboard.serve._pages._delete_confirm import (
    DeleteConfirmIds,
    delete_confirm_modal,
    register_delete_callbacks,
)
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._url import relative_path

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dlboard.models import DataStore

TITLE_ID: DivId[ids.ComparisonPage] = DivId("comparison-title")
DESCRIPTION_ID: DivId[ids.ComparisonPage] = DivId("comparison-description")
SHARED_BADGE_ID: DivId[ids.ComparisonPage] = DivId("comparison-shared")
EDIT_OPEN_ID: ButtonId[ids.ComparisonPage] = ButtonId("comparison-edit-open")
EDIT_MODAL_ID: ModalId[ids.ComparisonPage] = ModalId("comparison-edit-modal")
EDIT_NAME_ID: ValueId[ids.ComparisonPage] = ValueId("comparison-edit-name")
EDIT_DESCRIPTION_ID: ValueId[ids.ComparisonPage] = ValueId("comparison-edit-description")
EDIT_SAVE_ID: ButtonId[ids.ComparisonPage] = ButtonId("comparison-edit-save")
EDIT_CANCEL_ID: ButtonId[ids.ComparisonPage] = ButtonId("comparison-edit-cancel")
SHARE_ID: ButtonId[ids.ComparisonPage] = ButtonId("comparison-share")
DUPLICATE_ID: ButtonId[ids.ComparisonPage] = ButtonId("comparison-duplicate")
_OPEN_ON_CLICK_JS = ClientsideScript(Path(__file__).with_name("open_on_click.js"))

DELETE_IDS = DeleteConfirmIds(
    button=ButtonId[ids.ComparisonPage]("comparison-delete"),
    modal=ModalId[ids.ComparisonPage]("comparison-delete-modal"),
    confirm=ButtonId[ids.ComparisonPage]("comparison-delete-confirm"),
    cancel=ButtonId[ids.ComparisonPage]("comparison-delete-cancel"),
)

_NO_DESCRIPTION: typing.Final = "No description"


def _share_label(*, shared: bool) -> str:
    return "Stop sharing with the project" if shared else "Share with the project"


def _shared_text(*, shared: bool) -> str:
    return "Shared with the project" if shared else "Private"


def comparison_header(store: DataStore[...], page: page_model.BasicExperimentPage) -> Component:
    """The header of the comparison `page`: for its owner, with the controls to rename, share and delete it."""
    summary = store.comparison_summary(page.id)
    is_owner = page.owner_id == get_current_user().id
    return html.Div(
        [
            dmc.Group(
                [
                    dmc.Stack(
                        [
                            dmc.Group(
                                [
                                    dmc.Title(page.name, order=3, id=TITLE_ID),
                                    *(
                                        [
                                            tooltipped_action_icon(
                                                Icon.EDIT,
                                                component_id=EDIT_OPEN_ID,
                                                label="Edit name and description",
                                            )
                                        ]
                                        if is_owner
                                        else []
                                    ),
                                ],
                                gap="xs",
                            ),
                            dmc.Text(
                                page.document.description or _NO_DESCRIPTION,
                                c="dimmed",
                                size="sm",
                                id=DESCRIPTION_ID,
                            ),
                            dmc.Group(
                                [
                                    dmc.Text(
                                        f"by {summary.owner_name}" if summary else "", size="xs", c="dimmed"
                                    ),
                                    dmc.Badge(
                                        _shared_text(shared=page.shared),
                                        id=SHARED_BADGE_ID,
                                        variant="light",
                                        size="sm",
                                    ),
                                    *(
                                        []
                                        if is_owner
                                        else [
                                            dmc.Badge(
                                                "Your edits save to a copy of your own",
                                                variant="light",
                                                color="yellow",
                                                size="sm",
                                            )
                                        ]
                                    ),
                                ],
                                gap="xs",
                            ),
                        ],
                        gap=2,
                    ),
                    dmc.Menu(
                        [
                            dmc.MenuTarget(
                                dmc.ActionIcon(
                                    icon(Icon.MORE),
                                    variant="subtle",
                                    color="gray",
                                    **typing.cast("dict[str, Any]", {"aria-label": "Comparison actions"}),
                                )
                            ),
                            dmc.MenuDropdown(
                                [
                                    *(
                                        [
                                            dmc.MenuItem(
                                                _share_label(shared=page.shared),
                                                id=SHARE_ID,
                                                leftSection=icon(Icon.LINK),
                                            )
                                        ]
                                        if is_owner
                                        else []
                                    ),
                                    dmc.MenuItem(
                                        "Duplicate to a copy of your own",
                                        id=DUPLICATE_ID,
                                        leftSection=icon(Icon.COPY),
                                    ),
                                    *(
                                        [
                                            dmc.MenuDivider(),
                                            dmc.MenuItem(
                                                "Delete this comparison…",
                                                id=DELETE_IDS.button,
                                                leftSection=icon(Icon.DELETE),
                                                color="red",
                                            ),
                                        ]
                                        if is_owner
                                        else []
                                    ),
                                ]
                            ),
                        ],
                        position="bottom-end",
                        withinPortal=True,
                    ),
                ],
                justify="space-between",
                align="flex-start",
                wrap="nowrap",
            ),
            _edit_modal(page),
            delete_confirm_modal(
                DELETE_IDS,
                entity_noun="comparison",
                body="This deletes the comparison and its list of runs. The runs themselves are untouched.",
            ),
        ]
    )


def _edit_modal(page: page_model.BasicExperimentPage) -> Component:
    return dmc.Modal(
        id=EDIT_MODAL_ID,
        title="Edit comparison",
        opened=False,
        children=[
            dmc.TextInput(id=EDIT_NAME_ID, label="Name", value=page.name),
            dmc.Textarea(
                id=EDIT_DESCRIPTION_ID,
                label="Description",
                value=page.document.description,
                minRows=3,
                autosize=True,
                mt="sm",
            ),
            dmc.Group(
                [
                    dmc.Button("Cancel", id=EDIT_CANCEL_ID, variant="default"),
                    dmc.Button("Save", id=EDIT_SAVE_ID),
                ],
                justify="flex-end",
                mt="md",
            ),
        ],
    )


def _stored(store: DataStore[...], page_id: int) -> page_model.BasicExperimentPage:
    return repo.load_page(store, repo.ComparisonRef(page_id))


class _SaveEdit(CallbackStates):
    clicks: Annotated[int | None, Input(EDIT_SAVE_ID, "n_clicks")]
    name: Annotated[str | None, State(EDIT_NAME_ID, "value")]
    description: Annotated[str | None, State(EDIT_DESCRIPTION_ID, "value")]
    page_id: Annotated[int, State(ids.COMPARISON_ID_STORE, "data")]


def register_header_callbacks(app: Dash) -> None:
    """Edit, share, duplicate and delete: what a comparison's header offers."""

    @app.callback(
        Output(EDIT_MODAL_ID, "opened", allow_duplicate=True),
        Input(EDIT_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_edit(n_clicks: int | None) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False

    app.clientside_callback(
        _OPEN_ON_CLICK_JS.source,
        Output(EDIT_MODAL_ID, "opened", allow_duplicate=True),
        Input(EDIT_OPEN_ID, "n_clicks"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output(EDIT_MODAL_ID, "opened", allow_duplicate=True),
        Output(TITLE_ID, "children"),
        Output(DESCRIPTION_ID, "children"),
        _SaveEdit.states(),
        prevent_initial_call=True,
    )
    def save_edit(
        **states: Any,  # noqa: ANN401 -- validated on the first line
    ) -> tuple[bool, str, str]:
        saved = _SaveEdit.model_validate(states)
        if not saved.clicks:
            raise PreventUpdate
        store = get_data_store()
        page = _stored(store, saved.page_id)
        description = (saved.description or "").strip()
        renamed = page.with_settings(page_model.DocumentSettings(description=description)).model_copy(
            update={"name": (saved.name or "").strip() or page.name}
        )
        updated = store.update_page(renamed)
        return False, updated.name, description or _NO_DESCRIPTION

    @app.callback(
        Output(SHARE_ID, "children"),
        Output(SHARED_BADGE_ID, "children"),
        Input(SHARE_ID, "n_clicks"),
        State(ids.COMPARISON_ID_STORE, "data"),
        prevent_initial_call=True,
    )
    def toggle_sharing(n_clicks: int | None, page_id: int) -> tuple[str, str]:
        if not n_clicks:
            raise PreventUpdate
        store = get_data_store()
        page = _stored(store, page_id)
        shared = not page.shared
        store.update_page(page.model_copy(update={"shared": shared}))
        return _share_label(shared=shared), _shared_text(shared=shared)

    @app.callback(
        Output(constants.LOCATION_ID, "href", allow_duplicate=True),
        Input(DUPLICATE_ID, "n_clicks"),
        State(ids.COMPARISON_ID_STORE, "data"),
        prevent_initial_call=True,
    )
    def duplicate(n_clicks: int | None, page_id: int) -> str:
        if not n_clicks:
            raise PreventUpdate
        store = get_data_store()
        copy = repo.copy_comparison(store, _stored(store, page_id))
        return relative_path(f"/comparison/{copy.id}")

    def delete_comparison(page_id: int) -> str:
        store = get_data_store()
        page = _stored(store, page_id)
        store.delete_view(page_id, get_current_user().id)
        return relative_path(f"/project/{page.project_id}")

    register_delete_callbacks(
        app, DELETE_IDS, State(ids.COMPARISON_ID_STORE, "data"), on_confirm=delete_comparison
    )
