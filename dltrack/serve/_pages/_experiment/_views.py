"""
Personal views of an experiment page: named copies of its panels and settings, one user's own.

The shared page is what everyone sees. A view starts as a copy of whatever the page looks like
right now and from then on is edited on its own -- rearranging, filtering runs or retitling charts
in a view never changes the shared page, nor anyone else's view. The active view is part of the
URL (`?view=<id>`), so it survives a reload and can be bookmarked or sent to someone.

Views can also be exported as JSON and imported back (into this or another experiment), validated
by `ViewFile` on the way in.
"""

from __future__ import annotations

import typing
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import dash_mantine_components as dmc
from dash import Input, Output, State, html, no_update
from dash.exceptions import PreventUpdate
from pydantic import BaseModel, ValidationError

from dltrack import models
from dltrack.models import ButtonId, ModalId, constants
from dltrack.serve import ClientsideScript, Icon, get_current_user, get_data_store, icon
from dltrack.serve._pages._delete_confirm import (
    DeleteConfirmIds,
    register_delete_callbacks,
    render_delete_control,
)
from dltrack.serve._pages._experiment import _experiment_page_state as core

if TYPE_CHECKING:
    from dash import Dash
    from dash.development.base_component import Component

    from dltrack.models import DataStore

SHARED_VIEW: typing.Final = "shared"
"""The picker's value for the shared page (a view's value is its id)."""

VIEW_SELECT_ID: typing.Final = "view-select"
SAVE_VIEW_OPEN_ID: typing.Final = "save-view-open"
SAVE_VIEW_MODAL_ID: typing.Final = "save-view-modal"
SAVE_VIEW_NAME_ID: typing.Final = "save-view-name"
SAVE_VIEW_CONFIRM_ID: typing.Final = "save-view-confirm"
EXPORT_VIEW_OPEN_ID: typing.Final = "export-view-open"
EXPORT_VIEW_MODAL_ID: typing.Final = "export-view-modal"
EXPORT_VIEW_BODY_ID: typing.Final = "export-view-body"
IMPORT_VIEW_OPEN_ID: typing.Final = "import-view-open"
IMPORT_VIEW_MODAL_ID: typing.Final = "import-view-modal"
IMPORT_VIEW_JSON_ID: typing.Final = "import-view-json"
IMPORT_VIEW_CONFIRM_ID: typing.Final = "import-view-confirm"

DELETE_VIEW_IDS = DeleteConfirmIds(
    button=ButtonId[core.ExperimentPage]("delete-view-button"),
    modal=ModalId[core.ExperimentPage]("delete-view-modal"),
    confirm=ButtonId[core.ExperimentPage]("delete-view-confirm"),
    cancel=ButtonId[core.ExperimentPage]("delete-view-cancel"),
)

_NAVIGATE_JS = ClientsideScript(Path(__file__).with_name("view_navigate.js"))
_OPEN_ON_CLICK_JS = ClientsideScript(Path(__file__).with_name("open_on_click.js"))


class ViewFile(BaseModel, extra="forbid"):
    """A view's portable contents: what "Export" writes and "Import" reads back (and validates)."""

    dltrack_view: Literal[1] = 1
    """Format version, so a future format change can recognize (and migrate) an old file."""

    name: str
    panels: list[models.PanelInstance[Any, Any]]
    page_settings: dict[str, int | float | bool | str | list[str] | list[int] | None] = {}

    @classmethod
    def of(cls, page: models.NewPage[Any, Any], *, name: str) -> ViewFile:
        return cls(name=name, panels=page.panels, page_settings=page.page_settings)


def _options(store: DataStore[...], experiment_id: int) -> list[dict[str, str]]:
    views = store.list_views(experiment_id, get_current_user(store).id)
    return [
        {"value": SHARED_VIEW, "label": "Shared view"},
        *({"value": str(v.id), "label": v.name} for v in views),
    ]


def resolve_view_id(store: DataStore[...], experiment_id: int, requested: str | None) -> int | None:
    """The id of the view a `?view=` value names, if it's really a view of this experiment."""
    if not requested or not requested.isdigit():
        return None
    view = store.get_view(core.BasicExperimentPage, int(requested))
    return view.id if view is not None and view.experiment_id == experiment_id else None


def view_controls(store: DataStore[...], experiment_id: int, view_id: int | None) -> Component:
    """The header's view picker, its actions menu, and (in a view) a delete button."""
    return dmc.Group(
        [
            dmc.Select(
                id=VIEW_SELECT_ID,
                data=_options(store, experiment_id),
                value=str(view_id) if view_id is not None else SHARED_VIEW,
                allowDeselect=False,
                size="xs",
                w=180,
                **cast("dict[str, Any]", {"aria-label": "View"}),
            ),
            dmc.Menu(
                [
                    dmc.MenuTarget(
                        dmc.ActionIcon(
                            icon(Icon.MORE),
                            variant="subtle",
                            color="gray",
                            **cast("dict[str, Any]", {"aria-label": "View actions"}),
                        )
                    ),
                    dmc.MenuDropdown(
                        [
                            dmc.MenuItem(
                                "Save as a new view…", id=SAVE_VIEW_OPEN_ID, leftSection=icon(Icon.SAVE)
                            ),
                            dmc.MenuItem(
                                "Export as JSON", id=EXPORT_VIEW_OPEN_ID, leftSection=icon(Icon.EXPORT)
                            ),
                            dmc.MenuItem(
                                "Import from JSON…", id=IMPORT_VIEW_OPEN_ID, leftSection=icon(Icon.IMPORT)
                            ),
                        ]
                    ),
                ],
                position="bottom-end",
            ),
            *(
                render_delete_control(
                    DELETE_VIEW_IDS, label="Delete this view", entity_noun="view", icon_only=True
                )
                if view_id is not None
                else []
            ),
            _save_modal(),
            _export_modal(),
            _import_modal(),
        ],
        gap=4,
        wrap="nowrap",
    )


def _save_modal() -> dmc.Modal:
    return dmc.Modal(
        id=SAVE_VIEW_MODAL_ID,
        title="Save as a new view",
        opened=False,
        children=dmc.Stack(
            [
                dmc.Text(
                    "Saves the page as it looks now as your own view. Changes you make in it won't "
                    "affect the shared view.",
                    size="sm",
                    c="dimmed",
                ),
                dmc.TextInput(
                    id=SAVE_VIEW_NAME_ID,
                    placeholder="View name",
                    **cast("dict[str, Any]", {"data-autofocus": True, "aria-label": "View name"}),
                ),
                dmc.Group(dmc.Button("Save view", id=SAVE_VIEW_CONFIRM_ID), justify="flex-end"),
            ]
        ),
    )


def _export_modal() -> dmc.Modal:
    return dmc.Modal(
        id=EXPORT_VIEW_MODAL_ID,
        title="Export as JSON",
        size="lg",
        opened=False,
        children=html.Div(id=EXPORT_VIEW_BODY_ID),
    )


def _import_modal() -> dmc.Modal:
    return dmc.Modal(
        id=IMPORT_VIEW_MODAL_ID,
        title="Import a view from JSON",
        size="lg",
        opened=False,
        children=dmc.Stack(
            [
                dmc.Textarea(
                    id=IMPORT_VIEW_JSON_ID,
                    placeholder="Paste an exported view here",
                    autosize=True,
                    minRows=6,
                    maxRows=16,
                    ff="monospace",
                ),
                dmc.Group(dmc.Button("Import as a new view", id=IMPORT_VIEW_CONFIRM_ID), justify="flex-end"),
            ]
        ),
    )


def _create_view(
    store: DataStore[...], experiment_id: int, view: ViewFile
) -> tuple[list[dict[str, str]], str]:
    """Save `view` as the current user's, and return the picker's new options and value."""
    created = store.create_view(
        core.BasicExperimentPage,
        models.NewPage[Any, Any](
            experiment_id=experiment_id,
            owner_id=get_current_user(store).id,
            name=view.name,
            panels=view.panels,
            page_settings=view.page_settings,
        ),
    )
    return _options(store, experiment_id), str(created.id)


def register_view_callbacks(app: Dash) -> None:
    """Wire the view picker: switching views, and saving/exporting/importing/deleting them."""
    # Switching views is a client-side navigation to `?view=` (or back to no view), like a link.
    app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        _NAVIGATE_JS.source,
        Output(core.STATE_VIEW_ID, "data"),
        Input(VIEW_SELECT_ID, "value"),
        State(core.STATE_VIEW_ID, "data"),
        prevent_initial_call=True,
    )

    for opener, modal in [
        (SAVE_VIEW_OPEN_ID, SAVE_VIEW_MODAL_ID),
        (IMPORT_VIEW_OPEN_ID, IMPORT_VIEW_MODAL_ID),
    ]:
        app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
            _OPEN_ON_CLICK_JS.source,
            Output(modal, "opened", allow_duplicate=True),
            Input(opener, "n_clicks"),
            prevent_initial_call=True,
        )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(VIEW_SELECT_ID, "data", allow_duplicate=True),
        Output(VIEW_SELECT_ID, "value", allow_duplicate=True),
        Output(SAVE_VIEW_MODAL_ID, "opened", allow_duplicate=True),
        Output(SAVE_VIEW_NAME_ID, "error"),
        Output(SAVE_VIEW_NAME_ID, "value"),
        Input(SAVE_VIEW_CONFIRM_ID, "n_clicks"),
        Input(SAVE_VIEW_NAME_ID, "n_submit"),
        State(SAVE_VIEW_NAME_ID, "value"),
        State(core.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def save_view(
        _clicks: int | None, _submits: int | None, name: str | None, page_json: str, experiment_id: int
    ) -> tuple[Any, ...]:
        if not name or not name.strip():
            return no_update, no_update, no_update, "Give the view a name", no_update
        page = core.BasicExperimentPage.model_validate_json(page_json)
        options, value = _create_view(get_data_store(), experiment_id, ViewFile.of(page, name=name.strip()))
        return options, value, False, None, ""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(EXPORT_VIEW_MODAL_ID, "opened"),
        Output(EXPORT_VIEW_BODY_ID, "children"),
        Input(EXPORT_VIEW_OPEN_ID, "n_clicks"),
        State(core.STATE_PAGE_STORAGE, "data"),
        State(VIEW_SELECT_ID, "data"),
        State(VIEW_SELECT_ID, "value"),
        prevent_initial_call=True,
    )
    def export_view(
        n_clicks: int | None, page_json: str, options: list[dict[str, str]], value: str
    ) -> tuple[bool, Component]:
        if not n_clicks:
            raise PreventUpdate
        page = core.BasicExperimentPage.model_validate_json(page_json)
        name = next((o["label"] for o in options if o["value"] == value), "Shared view")
        exported = ViewFile.of(page, name=name).model_dump_json(indent=2)
        return True, html.Div(
            [
                dmc.Code(exported, block=True, className="dl-snippet", mah=420, style={"overflow": "auto"}),
                dmc.CopyButton(
                    value=exported,
                    children=[icon(Icon.COPY), " Copy"],
                    copiedChildren=[icon(Icon.COPIED), " Copied"],
                    color="gray",
                    copiedColor="green",
                    variant="subtle",
                    size="compact-xs",
                    className="dl-snippet-copy",
                ),
            ],
            style={"position": "relative"},
        )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(VIEW_SELECT_ID, "data", allow_duplicate=True),
        Output(VIEW_SELECT_ID, "value", allow_duplicate=True),
        Output(IMPORT_VIEW_MODAL_ID, "opened", allow_duplicate=True),
        Output(IMPORT_VIEW_JSON_ID, "error"),
        Input(IMPORT_VIEW_CONFIRM_ID, "n_clicks"),
        State(IMPORT_VIEW_JSON_ID, "value"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def import_view(n_clicks: int | None, raw: str | None, experiment_id: int) -> tuple[Any, ...]:
        if not n_clicks:
            raise PreventUpdate
        try:
            view = ViewFile.model_validate_json(raw or "")
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"]) or "file"
            return no_update, no_update, no_update, f"Not a dltrack view ({where}: {first['msg']})"
        options, value = _create_view(get_data_store(), experiment_id, view)
        return options, value, False, None

    def _delete_view(view_id: int) -> str:
        store = get_data_store()
        view = store.get_view(core.BasicExperimentPage, view_id)
        store.delete_view(view_id, get_current_user(store).id)
        return f"/experiment/{view.experiment_id}" if view is not None else "/"

    register_delete_callbacks(
        app, DELETE_VIEW_IDS, State(core.STATE_VIEW_ID, "data"), on_confirm=_delete_view
    )
