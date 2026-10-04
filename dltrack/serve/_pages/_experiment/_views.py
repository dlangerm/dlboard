"""
Personal views of an experiment page: named copies of its panels and settings, one user's own.

The shared page is what everyone sees. A view starts as a copy of whatever the page looks like
right now and from then on is edited on its own -- rearranging, filtering runs or retitling charts
in a view never changes the shared page, nor anyone else's view. The active view is part of the
URL (`?view=<id>`), so it survives a reload and can be bookmarked or sent to someone.

A view doesn't have to be created up front, either: every edit anywhere on this page goes through
`_experiment_page_state.save_page`, which branches into a new view of your own the moment you edit
something you don't already own (the shared page, or someone else's view) -- so pruning the shared
page down to "just one chart type" never touches what anyone else sees, even mid-experiment, with
no separate "duplicate this view" step first. `sync_view_after_edit`/`sync_view_url.js`, below, are what
make that branch show up in the picker and the URL without a page reload.

Views can also be renamed, and exported as JSON and imported back (into this or another
experiment), validated by `ViewFile` on the way in.
"""

from __future__ import annotations

import typing
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import dash_mantine_components as dmc
from dash import Input, NoUpdate, Output, State, html, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from pydantic import BaseModel, ValidationError

from dltrack import models
from dltrack.serve import ClientsideScript, Icon, get_current_user, get_data_store, icon
from dltrack.serve import _constants as constants
from dltrack.serve._component_ids import ButtonId, ModalId
from dltrack.serve._pages._delete_confirm import (
    DeleteConfirmIds,
    delete_confirm_modal,
    register_delete_callbacks,
)
from dltrack.serve._pages._experiment import _experiment_page_state as core

if TYPE_CHECKING:
    from dash import Dash
    from dash.development.base_component import Component

    from dltrack.models import DataStore

SHARED_VIEW: typing.Final = "shared"
"""The picker's value for the shared page (a view's value is its id)."""

VIEW_SELECT_ID: typing.Final = "view-select"
VIEW_NAV_SUPPRESS_ID: typing.Final = "view-nav-suppress"
"""
One-shot flag: `sync_view_after_edit` sets it right before writing `VIEW_SELECT_ID.value` itself
(not from a real pick), so `_register_switching`'s navigate-on-select callback -- which can't
otherwise tell "the user picked a view" from "an edit just branched into one" apart, since both
show up as the exact same prop change -- knows to skip navigating that once instead of pushing a
reload-triggering `?view=` change out from under whatever the edit itself already did.
"""
VIEW_NOTIFY_ID: typing.Final = "view-notify"
"""Toasts about views -- chiefly that an edit just branched into a new view of your own."""
VIEW_DUPLICATED_ID: typing.Final = "view-duplicated"
"""
One-shot flag: "Duplicate this view" sets it alongside its new page, so `sync_view_after_edit` -- which
sees that new page exactly as it sees an edit branching into one -- knows you named this one
yourself and skips the "saved to your own view" toast it would otherwise show.
"""
VIEW_FORK_HINT_ID: typing.Final = "view-fork-hint"
"""The "edits save to a copy" badge, shown only while the page on screen isn't yours."""
VIEW_MENU_ID: typing.Final = "view-menu-dropdown"
"""
The actions menu's dropdown. Its `className` says whether you own the view on screen, which
`_experiment_page_hover.css` uses to show or hide the owner-only items (rename, delete). It's a
class rather than a rebuilt menu so `sync_view_after_edit` can update it when an edit branches
into a view of your own, with no reload.
"""
SHARE_VIEW_TOGGLE_ID: typing.Final = "share-view-toggle"
"""Owner-only menu item that flips a view's `shared` flag; its own label reflects the new state."""
DUPLICATE_VIEW_OPEN_ID: typing.Final = "duplicate-view-open"
DUPLICATE_VIEW_MODAL_ID: typing.Final = "duplicate-view-modal"
DUPLICATE_VIEW_NAME_ID: typing.Final = "duplicate-view-name"
DUPLICATE_VIEW_CONFIRM_ID: typing.Final = "duplicate-view-confirm"
RENAME_VIEW_OPEN_ID: typing.Final = "rename-view-open"
RENAME_VIEW_MODAL_ID: typing.Final = "rename-view-modal"
RENAME_VIEW_NAME_ID: typing.Final = "rename-view-name"
RENAME_VIEW_CONFIRM_ID: typing.Final = "rename-view-confirm"
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
_SYNC_VIEW_URL_JS = ClientsideScript(Path(__file__).with_name("sync_view_url.js"))


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


def _options(
    store: DataStore[...], experiment_id: int, *, foreign_view: tuple[int, str] | None = None
) -> list[dict[str, str]]:
    """
    The picker's options: the shared page, then every view *you* own.

    `foreign_view` (the id and name of a view you're currently looking at but don't own, reached by
    a link someone else sent you) is appended too, if it isn't already one of your own -- otherwise
    the picker would have nothing to show for the view you're actually on.
    """
    views = store.list_views(experiment_id, get_current_user().id)
    options = [
        {"value": SHARED_VIEW, "label": "Shared view"},
        *({"value": str(v.id), "label": v.name} for v in views),
    ]
    if foreign_view is not None and not any(o["value"] == str(foreign_view[0]) for o in options):
        options.append({"value": str(foreign_view[0]), "label": foreign_view[1]})
    return options


def resolve_view_id(store: DataStore[...], experiment_id: int, requested: str | None) -> int | None:
    """The id of the view a `?view=` value names, if it's really a view of this experiment."""
    if not requested or not requested.isdigit():
        return None
    view = store.get_view(core.BasicExperimentPage, int(requested))
    return view.id if view is not None and view.experiment_id == experiment_id else None


def _menu_class(*, is_owner: bool) -> str:
    return "dl-view-owned" if is_owner else ""


def _fork_hint_style(*, is_owner: bool) -> dict[str, str]:
    """Always in the layout (a callback writes to it), just hidden once the page on screen is yours."""
    return {"display": "none"} if is_owner else {}


def _share_label(*, is_shared: bool) -> str:
    return "Stop sharing with project" if is_shared else "Share with project"


def view_controls(  # noqa: PLR0913
    store: DataStore[...],
    experiment_id: int,
    view_id: int | None,
    *,
    is_owner: bool,
    view_name: str | None = None,
    is_shared: bool = False,
) -> Component:
    """
    The header's view picker and its actions menu (rename/delete included, in a view of your own).

    `is_owner` is `False` for the shared page (nobody "owns" it) and for a view someone else's --
    editing either still works, it just branches you into a view of your own the moment you make a
    change (see `_experiment_page_state.save_page`) rather than letting you rename or delete theirs.
    `view_name` is the current page's own name, needed only to show a view that isn't yours (`not
    is_owner`, `view_id` not `None`) in the picker at all -- see `_options`'s `foreign_view`.
    `is_shared` is the current page's own `NewPage.shared` -- meaningless (and the toggle hidden)
    unless `is_owner`, same as rename/delete.
    """
    owner_only = "dl-view-owner-only"
    return dmc.Group(
        [
            dmc.Select(
                id=VIEW_SELECT_ID,
                data=_options(
                    store,
                    experiment_id,
                    foreign_view=(view_id, view_name or "") if view_id is not None and not is_owner else None,
                ),
                value=str(view_id) if view_id is not None else SHARED_VIEW,
                allowDeselect=False,
                size="xs",
                w=180,
                **cast("dict[str, Any]", {"aria-label": "View"}),
            ),
            # The wrapper (not the badge) is what's hidden: a hidden badge inside its tooltip still
            # left a gap in this group, nudging the picker 4px left on every view you own.
            html.Div(
                dmc.Tooltip(
                    dmc.Badge("Edits save to a copy", variant="light", color="gray", size="sm"),
                    label="This view isn't yours, so your first change is saved as a new view of your own. "
                    "The original stays as it is.",
                    multiline=True,
                    w=260,
                    withArrow=True,
                ),
                id=VIEW_FORK_HINT_ID,
                style=_fork_hint_style(is_owner=is_owner),
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
                                "Rename this view…",
                                id=RENAME_VIEW_OPEN_ID,
                                leftSection=icon(Icon.EDIT),
                                className=owner_only,
                            ),
                            dmc.MenuItem(
                                _share_label(is_shared=is_shared),
                                id=SHARE_VIEW_TOGGLE_ID,
                                leftSection=icon(Icon.LINK),
                                className=owner_only,
                            ),
                            dmc.MenuItem(
                                "Duplicate this view…", id=DUPLICATE_VIEW_OPEN_ID, leftSection=icon(Icon.COPY)
                            ),
                            dmc.MenuItem(
                                "Export as JSON", id=EXPORT_VIEW_OPEN_ID, leftSection=icon(Icon.EXPORT)
                            ),
                            dmc.MenuItem(
                                "Import from JSON…", id=IMPORT_VIEW_OPEN_ID, leftSection=icon(Icon.IMPORT)
                            ),
                            dmc.MenuDivider(className=owner_only),
                            dmc.MenuItem(
                                "Delete this view…",
                                id=DELETE_VIEW_IDS.button,
                                leftSection=icon(Icon.DELETE),
                                color="red",
                                className=owner_only,
                            ),
                        ],
                        id=VIEW_MENU_ID,
                        className=_menu_class(is_owner=is_owner),
                    ),
                ],
                position="bottom-end",
            ),
            delete_confirm_modal(
                DELETE_VIEW_IDS,
                entity_noun="view",
                body="This deletes your view for good. The shared view and everyone else's views "
                "are left as they are.",
            ),
            _duplicate_modal(),
            _rename_modal(),
            _export_modal(),
            _import_modal(),
            Store(id=VIEW_NAV_SUPPRESS_ID, data=False),
            Store(id=VIEW_DUPLICATED_ID, data=False),
            dmc.NotificationContainer(id=VIEW_NOTIFY_ID),
        ],
        gap=4,
        wrap="nowrap",
    )


def _duplicate_modal() -> dmc.Modal:
    return dmc.Modal(
        id=DUPLICATE_VIEW_MODAL_ID,
        title="Duplicate this view",
        opened=False,
        children=dmc.Stack(
            [
                dmc.Text(
                    "Copies the page as it looks now into a new view of your own. Changes you make in the "
                    "copy won't affect the original.",
                    size="sm",
                    c="dimmed",
                ),
                dmc.TextInput(
                    id=DUPLICATE_VIEW_NAME_ID,
                    placeholder="View name",
                    **cast("dict[str, Any]", {"data-autofocus": True, "aria-label": "View name"}),
                ),
                dmc.Group(dmc.Button("Duplicate", id=DUPLICATE_VIEW_CONFIRM_ID), justify="flex-end"),
            ]
        ),
    )


def _rename_modal() -> dmc.Modal:
    return dmc.Modal(
        id=RENAME_VIEW_MODAL_ID,
        title="Rename this view",
        opened=False,
        children=dmc.Stack(
            [
                dmc.TextInput(
                    id=RENAME_VIEW_NAME_ID,
                    placeholder="View name",
                    **cast("dict[str, Any]", {"data-autofocus": True, "aria-label": "View name"}),
                ),
                dmc.Group(dmc.Button("Save name", id=RENAME_VIEW_CONFIRM_ID), justify="flex-end"),
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


def _create_view(store: DataStore[...], experiment_id: int, view: ViewFile) -> models.Page[Any, Any, Any]:
    """Save `view` as the current user's."""
    return store.create_view(
        core.BasicExperimentPage,
        models.NewPage[Any, Any](
            experiment_id=experiment_id,
            owner_id=get_current_user().id,
            name=view.name,
            panels=view.panels,
            page_settings=view.page_settings,
        ),
    )


def _register_switching(app: Dash) -> None:
    # Switching views is a client-side navigation to `?view=` (or back to no view), like a link.
    app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        _NAVIGATE_JS.source,
        Output(core.STATE_VIEW_ID, "data"),
        Output(VIEW_NAV_SUPPRESS_ID, "data", allow_duplicate=True),
        Input(VIEW_SELECT_ID, "value"),
        State(core.STATE_VIEW_ID, "data"),
        State(VIEW_NAV_SUPPRESS_ID, "data"),
        prevent_initial_call=True,
    )

    for opener, modal in [
        (DUPLICATE_VIEW_OPEN_ID, DUPLICATE_VIEW_MODAL_ID),
        (IMPORT_VIEW_OPEN_ID, IMPORT_VIEW_MODAL_ID),
    ]:
        app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
            _OPEN_ON_CLICK_JS.source,
            Output(modal, "opened", allow_duplicate=True),
            Input(opener, "n_clicks"),
            prevent_initial_call=True,
        )


def _register_duplicate(app: Dash) -> None:
    # The new view looks exactly like the page on screen, so there's nothing to re-render: writing
    # it to `STATE_PAGE_STORAGE` hands off to `sync_view_after_edit`, the same as an edit branching
    # into a view of your own, which catches the picker, menu and URL up without a reload.
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(DUPLICATE_VIEW_MODAL_ID, "opened", allow_duplicate=True),
        Output(DUPLICATE_VIEW_NAME_ID, "error"),
        Output(DUPLICATE_VIEW_NAME_ID, "value"),
        Output(VIEW_DUPLICATED_ID, "data", allow_duplicate=True),
        Input(DUPLICATE_VIEW_CONFIRM_ID, "n_clicks"),
        Input(DUPLICATE_VIEW_NAME_ID, "n_submit"),
        State(DUPLICATE_VIEW_NAME_ID, "value"),
        State(core.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def duplicate_view(
        _clicks: int | None, _submits: int | None, name: str | None, page_json: str, experiment_id: int
    ) -> tuple[Any, ...]:
        if not name or not name.strip():
            return no_update, no_update, "Give the view a name", no_update, no_update
        page = core.BasicExperimentPage.model_validate_json(page_json)
        created = _create_view(get_data_store(), experiment_id, ViewFile.of(page, name=name.strip()))
        return created.model_dump_json(), False, None, "", True


def _register_rename(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(RENAME_VIEW_MODAL_ID, "opened", allow_duplicate=True),
        Output(RENAME_VIEW_NAME_ID, "value"),
        Input(RENAME_VIEW_OPEN_ID, "n_clicks"),
        State(VIEW_SELECT_ID, "value"),
        State(VIEW_SELECT_ID, "data"),
        prevent_initial_call=True,
    )
    def open_rename_view(n_clicks: int | None, value: str, options: list[dict[str, str]]) -> tuple[bool, str]:
        if not n_clicks:
            raise PreventUpdate
        current_name = next((o["label"] for o in options if o["value"] == value), "")
        return True, current_name

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(VIEW_SELECT_ID, "data", allow_duplicate=True),
        Output(RENAME_VIEW_MODAL_ID, "opened", allow_duplicate=True),
        Output(RENAME_VIEW_NAME_ID, "error"),
        Input(RENAME_VIEW_CONFIRM_ID, "n_clicks"),
        Input(RENAME_VIEW_NAME_ID, "n_submit"),
        State(RENAME_VIEW_NAME_ID, "value"),
        State(core.STATE_VIEW_ID, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def rename_view(
        _clicks: int | None, _submits: int | None, name: str | None, view_id: int | None, experiment_id: int
    ) -> tuple[Any, bool | NoUpdate, str | None]:
        if not name or not name.strip():
            return no_update, no_update, "Give the view a name"
        if view_id is None:
            raise PreventUpdate
        store = get_data_store()
        # Defensive, not just decorative -- `view_controls` only *shows* this to a view's owner,
        # but the callback itself is reachable regardless, so it checks again before writing.
        view = store.get_view(core.BasicExperimentPage, view_id)
        if view is None or view.owner_id != get_current_user().id:
            raise PreventUpdate
        store.update_page(view.model_copy(update={"name": name.strip()}))
        return _options(store, experiment_id), False, None


def _register_share_toggle(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(SHARE_VIEW_TOGGLE_ID, "children"),
        Input(SHARE_VIEW_TOGGLE_ID, "n_clicks"),
        State(core.STATE_VIEW_ID, "data"),
        prevent_initial_call=True,
    )
    def toggle_share(n_clicks: int | None, view_id: int | None) -> str:
        if not n_clicks or view_id is None:
            raise PreventUpdate
        store = get_data_store()
        # Defensive, not just decorative -- `view_controls` only *shows* this to a view's owner,
        # but the callback itself is reachable regardless, so it checks again before writing.
        view = store.get_view(core.BasicExperimentPage, view_id)
        if view is None or view.owner_id != get_current_user().id:
            raise PreventUpdate
        updated = store.update_page(view.model_copy(update={"shared": not view.shared}))
        return _share_label(is_shared=updated.shared)


def _register_export_import(app: Dash) -> None:
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
        store = get_data_store()
        created = _create_view(store, experiment_id, view)
        return _options(store, experiment_id), str(created.id), False, None


def _register_sync_after_edit(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(VIEW_SELECT_ID, "data", allow_duplicate=True),
        Output(VIEW_SELECT_ID, "value", allow_duplicate=True),
        Output(core.STATE_VIEW_ID, "data", allow_duplicate=True),
        Output(VIEW_NAV_SUPPRESS_ID, "data", allow_duplicate=True),
        Output(VIEW_MENU_ID, "className"),
        Output(VIEW_FORK_HINT_ID, "style"),
        Output(SHARE_VIEW_TOGGLE_ID, "children", allow_duplicate=True),
        Output(VIEW_NOTIFY_ID, "sendNotifications"),
        Output(VIEW_DUPLICATED_ID, "data", allow_duplicate=True),
        Input(core.STATE_PAGE_STORAGE, "data"),
        State(core.STATE_VIEW_ID, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(VIEW_DUPLICATED_ID, "data"),
        prevent_initial_call=True,
    )
    def sync_view_after_edit(
        page_json: str,
        current_view_id: int | None,
        experiment_id: int,
        duplicated: bool,  # noqa: FBT001
    ) -> tuple[Any, ...]:
        """
        Keep the picker and `STATE_VIEW_ID` in step with whichever page an edit actually landed in.

        Every persisting callback on this page writes its result to `STATE_PAGE_STORAGE` -- this is
        the one place that reacts to *any* of them, so an edit that branched into a new personal
        view (`_experiment_page_state.save_page`) shows up here without every one of those callbacks
        needing to know views exist at all. Cheap on the (overwhelmingly common) no-branch tick: the
        comparison below is pure Python, so the `list_views` read only happens on an actual branch.

        Arms `VIEW_NAV_SUPPRESS_ID` alongside its own `VIEW_SELECT_ID.value` write -- see that id's
        own docstring for why the picker's navigate-on-select callback needs it. Also reveals the
        menu's rename/delete/share items and hides the "edits save to a copy" badge, since the page
        on screen is now yours -- and says so in a toast, because a branch is otherwise silent: it's
        the edit you were making that created a view, and nothing on screen would tell you. Not for
        "Duplicate this view" (`VIEW_DUPLICATED_ID`), where you named the new view yourself.

        The share toggle's own label needs refreshing here too, same reasoning: a branch always
        creates a brand-new, private view (see `save_page`), so it may no longer match whatever this
        menu item said about the page you branched *from* (if that one happened to be shared).
        """
        page = core.BasicExperimentPage.model_validate_json(page_json)
        effective = page.id if page.owner_id is not None else None
        if effective == current_view_id:
            raise PreventUpdate
        store = get_data_store()
        value = str(effective) if effective is not None else SHARED_VIEW
        is_owner = page.owner_id == get_current_user().id
        toast: list[dict[str, Any]] | NoUpdate = no_update
        if is_owner and not duplicated:
            previous = (
                None if current_view_id is None else store.get_view(core.BasicExperimentPage, current_view_id)
            )
            original = f"“{previous.name}”" if previous is not None else "The shared view"
            toast = [
                {
                    "action": "show",
                    "id": f"view-fork-{page.id}",
                    "title": "Saved to your own view",
                    "message": f"Your change went into a new view, “{page.name}”. {original} is unchanged. "
                    "Rename your copy from the ⋯ menu.",
                    "color": "teal",
                    "autoClose": 10_000,
                }
            ]
        return (
            _options(store, experiment_id),
            value,
            effective,
            True,
            _menu_class(is_owner=is_owner),
            _fork_hint_style(is_owner=is_owner),
            _share_label(is_shared=page.shared),
            toast,
            False,
        )

    # The above changing `STATE_VIEW_ID` (without a page reload) still needs the address bar to
    # catch up, so a bookmark or a refresh keeps landing on the same branch -- purely client-side.
    app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        _SYNC_VIEW_URL_JS.source,
        Output(core.STATE_VIEW_ID, "data", allow_duplicate=True),
        Input(core.STATE_VIEW_ID, "data"),
        prevent_initial_call=True,
    )


def _register_delete(app: Dash) -> None:
    def _delete_view(view_id: int) -> str:
        store = get_data_store()
        view = store.get_view(core.BasicExperimentPage, view_id)
        store.delete_view(view_id, get_current_user().id)
        return f"/experiment/{view.experiment_id}" if view is not None else "/"

    register_delete_callbacks(
        app, DELETE_VIEW_IDS, State(core.STATE_VIEW_ID, "data"), on_confirm=_delete_view
    )


def register_view_callbacks(app: Dash) -> None:
    """Wire the view picker: switching views, and duplicating/renaming/sharing/exporting/importing/deleting them."""
    _register_switching(app)
    _register_duplicate(app)
    _register_rename(app)
    _register_share_toggle(app)
    _register_export_import(app)
    _register_sync_after_edit(app)
    _register_delete(app)
