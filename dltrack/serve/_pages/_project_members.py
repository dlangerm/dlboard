"""
A project's Sharing section: who can see or change it, managed by its owners.

Only rendered under an identity-verifying auth provider -- without one, everyone can already edit
every project, so there's nothing to share. Changes that add or remove a row hard-reload the page
rather than re-render in place, for the same reason the admin page's trash does: the rows hold
the very pattern-matched (`ALL`) controls those callbacks listen to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

import dash_mantine_components as dmc
from dash import ALL, Dash, Input, Output, State, ctx, no_update

from dltrack.models import ButtonId, DivId, NewProjectGrant, ProjectGrant, ProjectRole, ValueId, constants
from dltrack.serve import Icon, get_auth_provider, get_data_store, get_project_role, icon
from dltrack.serve._pages._dash_helpers import require_triggered_id, section_label, tooltipped_action_icon

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dltrack.models import Project, User


class _MembersSection:
    """Page tag: marks a component id as belonging to the project page's Sharing section."""


EVERYONE_ROLE_ID: ValueId[_MembersSection] = ValueId("project-everyone-role")
ADD_GRANTEE_ID: ValueId[_MembersSection] = ValueId("project-add-grantee")
ADD_ROLE_ID: ValueId[_MembersSection] = ValueId("project-add-role")
ADD_BUTTON_ID: ButtonId[_MembersSection] = ButtonId("project-add-member")
MEMBERS_ERROR_ID: DivId[_MembersSection] = DivId("project-members-error")
# Pattern-matched "type" discriminators, not standalone component ids -- plain strings.
MEMBER_ROLE_TYPE: Final = "project-member-role"
MEMBER_REMOVE_TYPE: Final = "project-member-remove"

GROUP_PREFIX: Final = "group:"
"""Typed before a name in the add field to grant an IdP group instead of a user, e.g. `group:ml-team`."""

_NOBODY: Final = "members-only"
_ROLE_OPTIONS: Final = [{"value": r.value, "label": r.value.capitalize()} for r in ProjectRole]
_EVERYONE_OPTIONS: Final = [
    {"value": _NOBODY, "label": "Only members"},
    {"value": ProjectRole.VIEWER.value, "label": "Everyone signed in can view"},
    {"value": ProjectRole.EDITOR.value, "label": "Everyone signed in can edit"},
]


def _grantee_label(grant: ProjectGrant, users: dict[int, User]) -> str:
    if grant.group is not None:
        return f"{GROUP_PREFIX}{grant.group}"
    user = users.get(grant.user_id) if grant.user_id is not None else None
    return user.username if user else f"User #{grant.user_id}"


def _member_row(grant: ProjectGrant, users: dict[int, User], *, can_manage: bool) -> Component:
    return dmc.TableTr(
        [
            dmc.TableTd(_grantee_label(grant, users)),
            dmc.TableTd(
                dmc.Select(
                    id={"type": MEMBER_ROLE_TYPE, "grant": grant.id},
                    data=_ROLE_OPTIONS,
                    value=grant.role.value,
                    disabled=not can_manage,
                    allowDeselect=False,
                    size="xs",
                    w=120,
                )
            ),
            dmc.TableTd(
                tooltipped_action_icon(
                    Icon.DELETE,
                    component_id={"type": MEMBER_REMOVE_TYPE, "grant": grant.id},
                    label="Remove",
                    disabled=not can_manage,
                )
            ),
        ]
    )


def render_members_section(project: Project) -> Component | None:
    """The Sharing section, or `None` when the deployment doesn't verify identity (and so can't share)."""
    if not get_auth_provider().verifies_identity:
        return None
    store = get_data_store()
    can_manage = get_project_role(project.id) is ProjectRole.OWNER
    users = {u.id: u for u in store.list_users()}
    grants = store.list_project_grants([project.id])
    add_row = dmc.Group(
        [
            dmc.TextInput(
                id=ADD_GRANTEE_ID,
                placeholder=f"username, or {GROUP_PREFIX}name",
                w=240,
                size="xs",
                **cast("dict[str, Any]", {"aria-label": "Add a member"}),
            ),
            dmc.Select(
                id=ADD_ROLE_ID,
                data=_ROLE_OPTIONS,
                value=ProjectRole.VIEWER.value,
                allowDeselect=False,
                size="xs",
                w=120,
            ),
            dmc.Button("Add", id=ADD_BUTTON_ID, size="xs", leftSection=icon(Icon.ADD)),
        ],
        gap="xs",
    )
    return dmc.Stack(
        [
            section_label("Sharing"),
            dmc.Select(
                id=EVERYONE_ROLE_ID,
                data=_EVERYONE_OPTIONS,
                value=project.everyone_role.value if project.everyone_role else _NOBODY,
                disabled=not can_manage,
                allowDeselect=False,
                size="xs",
                w=260,
            ),
            dmc.Table(
                dmc.TableTbody([_member_row(g, users, can_manage=can_manage) for g in grants]),
                withTableBorder=True,
                maw=520,
            ),
            *([add_row] if can_manage else []),
            dmc.Text(id=MEMBERS_ERROR_ID, c="red", size="sm"),
        ],
        gap="sm",
        mt="xl",
    )


def _new_grant(project_id: int, grantee: str, role: ProjectRole) -> NewProjectGrant | str:
    """The grant `grantee` (a username, or `group:<name>`) describes, or why it can't be made."""
    if grantee.startswith(GROUP_PREFIX):
        group = grantee.removeprefix(GROUP_PREFIX).strip()
        return NewProjectGrant(project_id=project_id, group=group, role=role) if group else "Name a group"
    user = get_data_store().find_user(grantee)
    if user is None:
        return f"No user named {grantee!r}"
    return NewProjectGrant(project_id=project_id, user_id=user.id, role=role)


def register_members_callbacks(app: Dash) -> None:
    """Wire the Sharing section's controls."""
    reload_outputs = (
        Output(constants.LOCATION_ID, "href", allow_duplicate=True),
        Output(constants.LOCATION_ID, "refresh", allow_duplicate=True),
    )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(MEMBERS_ERROR_ID, "children", allow_duplicate=True),
        Input(EVERYONE_ROLE_ID, "value"),
        State(constants.STATE_PROJECT_ID, "data"),
        prevent_initial_call=True,
    )
    def set_everyone_role(value: str, project_id: int) -> str:
        store = get_data_store()
        project = store.get_project(int(project_id))
        if project is None:
            return "Project not found"
        everyone = None if value == _NOBODY else ProjectRole(value)
        store.update_project(project.model_copy(update={"everyone_role": everyone}))
        return ""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(MEMBERS_ERROR_ID, "children", allow_duplicate=True),
        Input({"type": MEMBER_ROLE_TYPE, "grant": ALL}, "value"),
        State(constants.STATE_PROJECT_ID, "data"),
        prevent_initial_call=True,
    )
    def change_role(_values: list[str], project_id: int) -> str:
        grant_id = int(cast("dict[str, int]", require_triggered_id())["grant"])
        store = get_data_store()
        grant = next((g for g in store.list_project_grants([int(project_id)]) if g.id == grant_id), None)
        if grant is None:
            return "That member was already removed"
        role = ProjectRole(ctx.triggered[0]["value"])
        store.set_project_grant(
            NewProjectGrant.model_validate(grant.model_dump(exclude={"id"}) | {"role": role})
        )
        return ""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        *reload_outputs,
        Input({"type": MEMBER_REMOVE_TYPE, "grant": ALL}, "n_clicks"),
        State(constants.STATE_PROJECT_ID, "data"),
        prevent_initial_call=True,
    )
    def remove_member(_clicks: list[int], project_id: int) -> tuple[str, bool]:
        grant_id = int(cast("dict[str, int]", require_triggered_id())["grant"])
        get_data_store().delete_project_grant(int(project_id), grant_id)
        return f"/project/{project_id}", True

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        *reload_outputs,
        Output(MEMBERS_ERROR_ID, "children", allow_duplicate=True),
        Input(ADD_BUTTON_ID, "n_clicks"),
        Input(ADD_GRANTEE_ID, "n_submit"),
        State(ADD_GRANTEE_ID, "value"),
        State(ADD_ROLE_ID, "value"),
        State(constants.STATE_PROJECT_ID, "data"),
        prevent_initial_call=True,
    )
    def add_member(
        _clicks: int, _submits: int, grantee: str | None, role: str, project_id: int
    ) -> tuple[Any, Any, str]:
        grant = _new_grant(int(project_id), (grantee or "").strip(), ProjectRole(role))
        if isinstance(grant, str):
            return no_update, no_update, grant
        get_data_store().set_project_grant(grant)
        return f"/project/{project_id}", True, ""
