"""
The comparison page's header: its name and description, whose it is, and what its owner and anyone else can do with it.

The pieces that change (the title, the description, the share toggle's label) are updated in place, never drawn again with
their buttons: Dash calls a callback listening to a component as it is drawn by another callback, with nothing clicked.
"""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING, Any

import dash_mantine_components as dmc
from dash import Dash, Input, Output, State
from dash.exceptions import PreventUpdate

from dlboard.serve import Icon, get_current_user, get_data_store, icon
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import ButtonId, DivId, ModalId
from dlboard.serve._pages._comparison import _ids as ids
from dlboard.serve._pages._delete_confirm import (
    DeleteConfirmIds,
    delete_confirm_modal,
    register_delete_callbacks,
)
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment._live_updates import live_controls
from dlboard.serve._pages._page_header import PageHeaderIds, page_header, register_page_header
from dlboard.serve._url import relative_path

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dlboard.models import DataStore

HEADER_IDS = PageHeaderIds[ids.ComparisonPage].of("comparison", name=True)
SHARED_BADGE_ID: DivId[ids.ComparisonPage] = DivId("comparison-shared")
SHARE_ID: ButtonId[ids.ComparisonPage] = ButtonId("comparison-share")
DUPLICATE_ID: ButtonId[ids.ComparisonPage] = ButtonId("comparison-duplicate")

DELETE_IDS = DeleteConfirmIds(
    button=ButtonId[ids.ComparisonPage]("comparison-delete"),
    modal=ModalId[ids.ComparisonPage]("comparison-delete-modal"),
    confirm=ButtonId[ids.ComparisonPage]("comparison-delete-confirm"),
    cancel=ButtonId[ids.ComparisonPage]("comparison-delete-cancel"),
)


def _share_label(*, shared: bool) -> str:
    return "Stop sharing with the project" if shared else "Share with the project"


def _shared_text(*, shared: bool) -> str:
    return "Shared with the project" if shared else "Private"


def comparison_header(store: DataStore[...], page: page_model.BasicExperimentPage) -> Component:
    """The header of the comparison `page`: for its owner, with the controls to rename, share and delete it."""
    summary = store.comparison_summary(page.id)
    is_owner = page.owner_id == get_current_user().id
    return page_header(
        HEADER_IDS,
        title=page.name,
        description=page.document.description,
        editable=is_owner,
        meta=[
            dmc.Group(
                [
                    dmc.Text(f"by {summary.owner_name}" if summary else "", size="xs", c="dimmed"),
                    dmc.Badge(
                        _shared_text(shared=page.shared), id=SHARED_BADGE_ID, variant="light", size="sm"
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
            )
        ],
        aside=[
            # Whether the charts are following what is logged, then what can be done with the comparison, on one line as on an
            # experiment's page.
            dmc.Group(
                [
                    *live_controls(),
                    dmc.Divider(orientation="vertical"),
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
                gap="xs",
                wrap="nowrap",
                style={"alignSelf": "center"},
            ),
            delete_confirm_modal(
                DELETE_IDS,
                entity_noun="comparison",
                body="This deletes the comparison and its list of runs. The runs themselves are untouched.",
            ),
        ],
    )


def _stored(store: DataStore[...], page_id: int) -> page_model.BasicExperimentPage:
    return repo.load_page(store, repo.ComparisonRef(page_id))


def register_header_callbacks(app: Dash) -> None:
    """Edit, share, duplicate and delete: what a comparison's header offers."""
    comparison = State(ids.COMPARISON_ID_STORE, "data")

    def fetch(page_id: int) -> tuple[str, str]:
        page = _stored(get_data_store(), page_id)
        return page.name, page.document.description

    def save(page_id: int, name: str | None, description: str) -> tuple[str, str]:
        store = get_data_store()
        page = _stored(store, page_id)
        renamed = page.with_settings(page_model.DocumentSettings(description=description)).model_copy(
            update={"name": (name or "").strip() or page.name}
        )
        return store.update_page(renamed).name, description

    register_page_header(app, HEADER_IDS, comparison, fetch=fetch, save=save)

    @app.callback(
        Output(SHARE_ID, "children"),
        Output(SHARED_BADGE_ID, "children"),
        Input(SHARE_ID, "n_clicks"),
        comparison,
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
        comparison,
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

    register_delete_callbacks(app, DELETE_IDS, comparison, on_confirm=delete_comparison)
