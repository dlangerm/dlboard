"""The admin page: a trash can (restore/purge soft-deleted entities) and an audit log viewer."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import dash_mantine_components as dmc
from dash import ALL, Dash, Input, Output, State, html
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from structlog.stdlib import get_logger

from dltrack.models import AuditAction, EntityType, Scope, constants, has_scope
from dltrack.plugins.backend import artifact_purge_worker
from dltrack.plugins.pages._dash_helpers import require_triggered_id
from dltrack.serve import get_auth_provider, get_current_user, get_data_store, get_installed_plugins

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dltrack.models import AuditLogEntry, DataStore, InstalledPlugin, User

_log = get_logger(__name__)

# One (label, entity type, lister, restorer) tuple per soft-deletable entity -- the single place
# that needs to grow if a new soft-deletable entity is ever added.
_TRASH_KINDS: tuple[tuple[str, EntityType], ...] = (
    ("Project", EntityType.PROJECT),
    ("Experiment", EntityType.EXPERIMENT),
    ("Run", EntityType.RUN),
    ("Artifact", EntityType.ARTIFACT),
)

_LISTERS: dict[EntityType, str] = {
    EntityType.PROJECT: "list_deleted_projects",
    EntityType.EXPERIMENT: "list_deleted_experiments",
    EntityType.RUN: "list_deleted_runs",
    EntityType.ARTIFACT: "list_deleted_artifacts",
}
_RESTORERS: dict[EntityType, str] = {
    EntityType.PROJECT: "restore_project",
    EntityType.EXPERIMENT: "restore_experiment",
    EntityType.RUN: "restore_run",
    EntityType.ARTIFACT: "restore_artifact",
}
_PURGERS: dict[EntityType, str] = {
    EntityType.PROJECT: "purge_project",
    EntityType.EXPERIMENT: "purge_experiment",
    EntityType.RUN: "purge_run",
    EntityType.ARTIFACT: "purge_artifact",
}


def _trash_row(label: str, entity_type: EntityType, item: Any) -> Component:  # noqa: ANN401
    deleted_by = f" by user {item.deleted_by}" if item.deleted_by is not None else ""
    return dmc.Paper(
        dmc.Group(
            [
                dmc.Stack(
                    [
                        dmc.Text(f"{label} {item.id}", fw=600, size="sm"),
                        dmc.Text(f"deleted {item.deleted_at}{deleted_by}", c="dimmed", size="xs"),
                    ],
                    gap=2,
                ),
                dmc.Group(
                    [
                        dmc.Button(
                            "Restore",
                            id={
                                "type": constants.ADMIN_RESTORE_BUTTON_TYPE,
                                "entity_type": entity_type.value,
                                "id": item.id,
                            },
                            size="xs",
                            variant="light",
                        ),
                        dmc.Button(
                            "Purge",
                            id={
                                "type": constants.ADMIN_PURGE_BUTTON_TYPE,
                                "entity_type": entity_type.value,
                                "id": item.id,
                            },
                            size="xs",
                            color="red",
                            variant="light",
                        ),
                    ],
                    gap="xs",
                ),
            ],
            justify="space-between",
            wrap="nowrap",
        ),
        withBorder=True,
        radius="md",
        p="sm",
    )


_TRASH_PAGE_SIZE_PER_KIND = 25
"""
Cap per entity kind, not a global cap: a project's cascade can soft-delete thousands of artifact
rows in one go (every logged step of a training-heavy run), and rendering that many rows at once
is what made the trash tab unusable -- this keeps the DOM small (at most 4 * 25 rows) regardless
of how much history a single delete touched.
"""


def _render_trash_kind(label: str, entity_type: EntityType, items: list[Any]) -> Component:
    """
    One grouped section of the trash: a heading, then that kind's rows.

    Adds a "showing N most recent" note when the page cap was hit.
    """
    heading = f"{label}s ({len(items)}{'+' if len(items) == _TRASH_PAGE_SIZE_PER_KIND else ''})"
    return dmc.Stack(
        [
            dmc.Text(heading, fw=700, size="sm", tt="uppercase", c="dimmed"),
            *[_trash_row(label, entity_type, item) for item in items],
            *(
                [
                    dmc.Text(
                        f"Showing the {_TRASH_PAGE_SIZE_PER_KIND} most recently deleted -- "
                        "purge some to see older ones.",
                        c="dimmed",
                        size="xs",
                    )
                ]
                if len(items) == _TRASH_PAGE_SIZE_PER_KIND
                else []
            ),
        ],
        gap="xs",
    )


def _render_pending_purge_banner(store: DataStore[...]) -> Component | None:
    """
    An alert when a previous purge's blob cleanup hasn't fully finished.

    `None` (rendered as nothing) when there's no outstanding work -- the common case, since a
    purge that happens while the server is running is drained automatically.
    """
    pending = store.count_pending_artifact_purges()
    if not pending:
        return None
    return dmc.Alert(
        dmc.Group(
            [
                dmc.Text(
                    f"{pending} artifact blob{'s' if pending != 1 else ''} not yet cleaned up "
                    "from a previous purge.",
                    size="sm",
                ),
                dmc.Button("Resume cleanup", id=constants.ADMIN_RESUME_PURGE_ID, size="xs", variant="light"),
            ],
            justify="space-between",
        ),
        color="yellow",
        variant="light",
    )


def _render_trash(store: DataStore[...]) -> Component:
    sections: list[Component] = []
    banner = _render_pending_purge_banner(store)
    if banner is not None:
        sections.append(banner)
    for label, entity_type in _TRASH_KINDS:
        items = list(getattr(store, _LISTERS[entity_type])(limit=_TRASH_PAGE_SIZE_PER_KIND))
        if items:
            sections.append(_render_trash_kind(label, entity_type, items))
    if not sections:
        return dmc.Text("Nothing in the trash.", c="dimmed")
    return dmc.Stack(sections, gap="lg")


_AUDIT_ACTION_COLORS: dict[AuditAction, str] = {
    AuditAction.SOFT_DELETE: "orange",
    AuditAction.RESTORE: "teal",
    AuditAction.PURGE: "red",
}


def _format_audit_details(details: str) -> str:
    """Cascade counts as `Experiment: 3, Run: 12` instead of a raw JSON blob."""
    parsed = cast("dict[str, int]", json.loads(details))
    if not parsed:
        return "-"
    return ", ".join(f"{k}: {v}" for k, v in parsed.items())


def _audit_log_row(entry: AuditLogEntry) -> Component:
    return dmc.TableTr(
        [
            dmc.TableTd(
                f"{entry.timestamp_utc.isoformat(sep=' ', timespec='seconds')} UTC",
                style={"whiteSpace": "nowrap"},
            ),
            dmc.TableTd(f"User #{entry.user_id}"),
            dmc.TableTd(
                dmc.Badge(
                    entry.action.value.replace("_", " "),
                    color=_AUDIT_ACTION_COLORS[entry.action],
                    variant="light",
                    size="sm",
                )
            ),
            dmc.TableTd(f"{entry.entity_type.value.capitalize()} #{entry.entity_id}"),
            dmc.TableTd(_format_audit_details(entry.details), c="dimmed"),
        ]
    )


def _render_audit_log(store: DataStore[...], actor: User) -> Component:
    if not has_scope(actor, Scope.AUDIT_LOG_READ):
        return dmc.Text("You don't have permission to view the audit log.", c="dimmed")
    entries: list[AuditLogEntry] = list(store.list_audit_log(actor, limit=200))
    if not entries:
        return dmc.Text("No audit log entries yet.", c="dimmed")
    return dmc.Table(
        [
            dmc.TableThead(
                dmc.TableTr([dmc.TableTh(h) for h in ("Time", "User", "Action", "Entity", "Details")])
            ),
            dmc.TableTbody([_audit_log_row(entry) for entry in entries]),
        ],
        striped=True,
        highlightOnHover=True,
        withTableBorder=True,
        verticalSpacing="xs",
    )


def _plugin_row(plugin: InstalledPlugin) -> Component:
    return dmc.Paper(
        dmc.Stack(
            [
                dmc.Text(plugin.name, fw=600, size="sm", ff="monospace"),
                *([dmc.Text(plugin.description, c="dimmed", size="xs")] if plugin.description else []),
            ],
            gap=2,
        ),
        withBorder=True,
        radius="md",
        p="sm",
    )


def _render_about(
    store: DataStore[...], actor: User, auth_provider_name: str, plugins: list[InstalledPlugin]
) -> Component:
    return dmc.Stack(
        [
            dmc.Paper(
                dmc.Stack(
                    [
                        dmc.Text("Application", fw=700, size="sm", tt="uppercase", c="dimmed"),
                        dmc.Group(
                            [dmc.Text("Signed in as", size="sm"), dmc.Text(actor.username, fw=600, size="sm")]
                        ),
                        dmc.Group(
                            [
                                dmc.Text("Auth provider", size="sm"),
                                dmc.Badge(auth_provider_name, variant="light", color="gray", size="sm"),
                            ]
                        ),
                        dmc.Group(
                            [
                                dmc.Text("Storage backend", size="sm"),
                                dmc.Badge(
                                    getattr(store, "backend_name", store.__class__.__name__),
                                    variant="light",
                                    color="gray",
                                    size="sm",
                                ),
                            ]
                        ),
                    ],
                    gap="xs",
                ),
                withBorder=True,
                radius="md",
                p="md",
            ),
            dmc.Stack(
                [
                    dmc.Text("Installed plugins", fw=700, size="sm", tt="uppercase", c="dimmed"),
                    *[_plugin_row(p) for p in plugins],
                ],
                gap="xs",
            ),
        ],
        gap="lg",
    )


def _admin_layout() -> Component:
    return dmc.Container(
        [
            dmc.Title("Admin", order=2, fw=700, mb="md"),
            dmc.Tabs(
                [
                    dmc.TabsList(
                        [
                            dmc.TabsTab("Trash", value="trash"),
                            dmc.TabsTab("Audit Log", value="audit-log"),
                            dmc.TabsTab("About", value="about"),
                        ]
                    ),
                    dmc.TabsPanel(html.Div(id=constants.ADMIN_TRASH_CONTENT_ID), value="trash", pt="md"),
                    dmc.TabsPanel(
                        html.Div(id=constants.ADMIN_AUDIT_LOG_CONTENT_ID), value="audit-log", pt="md"
                    ),
                    dmc.TabsPanel(html.Div(id=constants.ADMIN_ABOUT_CONTENT_ID), value="about", pt="md"),
                ],
                id=constants.ADMIN_TABS_ID,
                value="trash",
            ),
            dmc.Modal(
                id=constants.ADMIN_PURGE_MODAL_ID,
                title="Permanently delete this?",
                opened=False,
                children=[
                    dmc.Text(
                        "This cannot be undone -- the item and everything under it will be "
                        "permanently removed, not just hidden."
                    ),
                    dmc.Group(
                        [
                            dmc.Button("Cancel", id=constants.ADMIN_PURGE_CANCEL_ID, variant="default"),
                            dmc.Button("Purge permanently", id=constants.ADMIN_PURGE_CONFIRM_ID, color="red"),
                        ],
                        justify="flex-end",
                        mt="sm",
                    ),
                ],
            ),
            Store(id=constants.ADMIN_PENDING_PURGE_STORE_ID),
        ],
        size="lg",
        py="xl",
    )


def _register_tab_callbacks(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.ADMIN_TRASH_CONTENT_ID, "children"),
        Input(constants.ADMIN_TABS_ID, "value"),
    )
    def render_trash_tab(tab: str) -> Component:
        if tab != "trash":
            raise PreventUpdate
        return _render_trash(get_data_store())

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.ADMIN_AUDIT_LOG_CONTENT_ID, "children"),
        Input(constants.ADMIN_TABS_ID, "value"),
    )
    def render_audit_log_tab(tab: str) -> Component:
        if tab != "audit-log":
            raise PreventUpdate
        store = get_data_store()
        return _render_audit_log(store, get_current_user(store))

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.ADMIN_ABOUT_CONTENT_ID, "children"),
        Input(constants.ADMIN_TABS_ID, "value"),
    )
    def render_about_tab(tab: str) -> Component:
        if tab != "about":
            raise PreventUpdate
        store = get_data_store()
        auth_provider_name = get_auth_provider().__class__.__name__
        return _render_about(store, get_current_user(store), auth_provider_name, get_installed_plugins())


def _register_restore_callback(app: Dash) -> None:
    # Hard-reloads back to `/admin` rather than re-rendering `ADMIN_TRASH_CONTENT_ID` in place:
    # that container holds the very pattern-matched Restore buttons this callback's `Input`
    # listens to (`ALL`), so an in-place re-render changes the matched-component set on every
    # firing, which makes Dash re-invoke the callback again -- a runaway render loop that hangs
    # the browser. A full reload sidesteps the self-reference entirely.
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.LOCATION_ID, "href", allow_duplicate=True),
        Output(constants.LOCATION_ID, "refresh", allow_duplicate=True),
        Input({"type": constants.ADMIN_RESTORE_BUTTON_TYPE, "entity_type": ALL, "id": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def restore_entity(_n_clicks_list: list[int]) -> tuple[str, bool]:
        triggered_id = cast("dict[str, str]", require_triggered_id())
        entity_type = EntityType(triggered_id["entity_type"])
        entity_id = int(triggered_id["id"])

        store = get_data_store()
        actor = get_current_user(store)
        try:
            getattr(store, _RESTORERS[entity_type])(entity_id, actor)
        except (ValueError, PermissionError):
            _log.exception("Failed to restore %s %s", entity_type, entity_id)
        return "/admin", True


def _register_purge_callbacks(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.ADMIN_PURGE_MODAL_ID, "opened", allow_duplicate=True),
        Output(constants.ADMIN_PENDING_PURGE_STORE_ID, "data"),
        Input({"type": constants.ADMIN_PURGE_BUTTON_TYPE, "entity_type": ALL, "id": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def open_purge_modal(_n_clicks_list: list[int]) -> tuple[bool, dict[str, Any]]:
        triggered_id = cast("dict[str, str]", require_triggered_id())
        return True, dict(triggered_id)

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.ADMIN_PURGE_MODAL_ID, "opened", allow_duplicate=True),
        Input(constants.ADMIN_PURGE_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_purge(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False

    # Hard-reloads back to `/admin` rather than re-rendering the trash list in place -- keeps this
    # symmetric with `restore_entity`, and avoids any similar reliance on in-place reconciliation
    # of dynamically-rendered content.
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.ADMIN_PURGE_MODAL_ID, "opened", allow_duplicate=True),
        Output(constants.LOCATION_ID, "href", allow_duplicate=True),
        Output(constants.LOCATION_ID, "refresh", allow_duplicate=True),
        Input(constants.ADMIN_PURGE_CONFIRM_ID, "n_clicks"),
        State(constants.ADMIN_PENDING_PURGE_STORE_ID, "data"),
        prevent_initial_call=True,
    )
    def confirm_purge(n_clicks: int, pending: dict[str, Any] | None) -> tuple[bool, str, bool]:
        if not n_clicks or not pending:
            raise PreventUpdate
        entity_type = EntityType(pending["entity_type"])
        entity_id = int(pending["id"])

        store = get_data_store()
        actor = get_current_user(store)
        try:
            getattr(store, _PURGERS[entity_type])(entity_id, actor)
        except (ValueError, PermissionError):
            _log.exception("Failed to purge %s %s", entity_type, entity_id)
        else:
            artifact_purge_worker.wake()
        return False, "/admin", True

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.LOCATION_ID, "href", allow_duplicate=True),
        Output(constants.LOCATION_ID, "refresh", allow_duplicate=True),
        Input(constants.ADMIN_RESUME_PURGE_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def resume_purge_cleanup(n_clicks: int) -> tuple[str, bool]:
        if not n_clicks:
            raise PreventUpdate
        artifact_purge_worker.wake()
        return "/admin", True


def plug(app: Dash) -> None:
    """An admin page: a Trash tab (restore/purge) and an Audit Log tab."""

    @app.callback(Output(constants.PAGE_ADMIN_ID, component_property="children"))  # pyright: ignore[reportUnknownMemberType]
    def layout() -> Component:
        return _admin_layout()

    _register_tab_callbacks(app)
    _register_restore_callback(app)
    _register_purge_callbacks(app)
