"""Tests for the admin page's pure rendering functions (trash list, audit log, layout)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from dlboard import models
from dlboard._version import __version__
from dlboard.conftest import find_props
from dlboard.plugins.themes import default
from dlboard.serve._backend._purge_work import blob_deletion, entity_purge
from dlboard.serve._pages import _simple_admin_page as admin

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


def test_render_trash_is_empty_message_when_nothing_deleted(store: SQLLiteStore) -> None:
    rendered = admin._render_trash(store, store)

    assert cast("Any", rendered).children == "Nothing in the trash."


def test_render_trash_lists_a_deleted_project_with_restore_and_purge_buttons(
    store: SQLLiteStore,
) -> None:
    admin_user = store.get_or_create_user(models.Principal.unverified("admin"))
    project = store.create_project(models.NewProject(name="p", description="d"))
    store.delete_project(project.id, admin_user)

    rendered = admin._render_trash(store, store)

    restore_id = {
        "type": admin.ADMIN_RESTORE_BUTTON_TYPE,
        "entity_type": models.EntityType.PROJECT.value,
        "id": project.id,
    }
    purge_id = {
        "type": admin.ADMIN_PURGE_BUTTON_TYPE,
        "entity_type": models.EntityType.PROJECT.value,
        "id": project.id,
    }
    assert find_props(rendered, restore_id) is not None
    assert find_props(rendered, purge_id) is not None


def test_render_trash_caps_each_kind_and_notes_the_overflow(store: SQLLiteStore) -> None:
    """A single cascade can soft-delete thousands of artifacts; the trash must never render all of them."""
    admin_user = store.get_or_create_user(models.Principal.unverified("admin"))
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    run = store.create_run(models.NewRun(experiment_id=experiment.id))
    over_the_cap = admin._TRASH_PAGE_SIZE_PER_KIND + 5
    for i in range(over_the_cap):
        store.log_artifact_refs(
            [
                models.Artifact(
                    key=f"a{i}",
                    fname=f"a{i}.png",
                    run_id=run.id,
                    experiment_id=experiment.id,
                    step=i,
                    ref="r",
                )
            ]
        )
    for artifact in store.fetch_artifacts(experiment_id=experiment.id, run_ids=None):
        assert artifact.id is not None
        store.delete_artifact(artifact.id, admin_user)

    rendered = cast("Any", admin._render_trash(store, store))

    # only the Artifact kind has any deleted items here; children are [heading, *rows, overflow note]
    (artifact_section,) = rendered.children
    heading, *rest = artifact_section.children
    *rows, overflow_note = rest
    assert "Artifacts" in heading.children
    assert len(rows) == admin._TRASH_PAGE_SIZE_PER_KIND
    assert "most recently deleted" in overflow_note.children


def test_render_pending_purge_banner_is_none_when_nothing_pending(store: SQLLiteStore) -> None:
    assert admin._render_pending_purge_banner(store) is None


@pytest.mark.parametrize(
    ("purges", "blobs", "says"),
    [
        (1, 0, "Queued: 1 purge."),
        (0, 2, "Queued: 2 artifact blobs to delete."),
        (1, 1, "1 purge and 1 artifact blob"),
    ],
)
def test_the_purge_banner_says_what_is_queued_for_the_background_tasks(
    store: SQLLiteStore, purges: int, blobs: int, says: str
) -> None:
    """Read from the database's work queue, so every server process says it, whichever custodian does the work."""
    store.enqueue_work(
        [
            *(entity_purge(models.EntityType.RUN, i, requested_by=1) for i in range(purges)),
            *(blob_deletion(i, f"ref://{i}", requested_by=1) for i in range(blobs)),
        ]
    )
    banner = admin._render_pending_purge_banner(store)
    assert says in str(banner)


def test_render_trash_omits_a_project_that_is_not_deleted(store: SQLLiteStore) -> None:
    store.create_project(models.NewProject(name="p", description="d"))

    rendered = admin._render_trash(store, store)

    assert cast("Any", rendered).children == "Nothing in the trash."


def test_render_audit_log_is_empty_message_with_no_entries(store: SQLLiteStore) -> None:
    admin_user = store.get_or_create_user(models.Principal.unverified("admin"))

    rendered = admin._render_audit_log(store, admin_user)

    assert cast("Any", rendered).children == "No audit log entries yet."


def test_render_audit_log_shows_a_permission_message_without_the_scope(store: SQLLiteStore) -> None:
    store.get_or_create_user(
        models.Principal.unverified("admin")
    )  # claim the bootstrap admin grant so the next user gets nothing
    no_scopes_user = store.get_or_create_user(models.Principal.unverified("nobody"))

    rendered = admin._render_audit_log(store, no_scopes_user)

    assert cast("Any", rendered).children == "You don't have permission to view the audit log."


def test_render_audit_log_lists_a_recorded_action(store: SQLLiteStore) -> None:
    admin_user = store.get_or_create_user(models.Principal.unverified("admin"))
    project = store.create_project(models.NewProject(name="p", description="d"))
    store.delete_project(project.id, admin_user)

    rendered = cast("Any", admin._render_audit_log(store, admin_user))

    # one row in the table body (`rendered.children` is [Thead, Tbody])
    body_rows = rendered.children[1].children  # pyrefly: ignore [unknown-variable-type]
    assert len(body_rows) == 1  # pyrefly: ignore [unknown-argument-type]
    action_cell = body_rows[0].children[2]  # pyrefly: ignore [unknown-variable-type]
    assert action_cell.children.children == "soft delete"


def test_render_about_shows_the_signed_in_user_provider_and_backend(store: SQLLiteStore) -> None:
    admin_user = store.get_or_create_user(models.Principal.unverified("admin"))

    rendered = admin._render_about(store, admin_user, "AnonymousAuthProvider", [])

    rendered_str = str(rendered)
    assert "admin" in rendered_str
    assert "AnonymousAuthProvider" in rendered_str
    assert "SQLLiteStore" in rendered_str
    assert __version__ in rendered_str


def test_render_about_lists_installed_plugins(store: SQLLiteStore) -> None:
    admin_user = store.get_or_create_user(models.Principal.unverified("admin"))
    # `admin` (the page module under test) is no longer a `PluginProtocol` -- the page move made
    # page layout non-optional dlboard behavior rather than a plugin, so `describe()` needs a real
    # plugin here instead. Any real plugin module works; `default` (the theme) is a small one.
    plugin = models.InstalledPlugin.describe(default)

    rendered = admin._render_about(store, admin_user, "AnonymousAuthProvider", [plugin])

    assert "dlboard.plugins.themes.default" in str(rendered)


def test_render_users_add_user_link_forces_a_full_page_load(store: SQLLiteStore) -> None:
    """
    The "Add a user" link points at a raw Flask route, not a Dash page -- without `refresh=True`,
    Dash's client-side router intercepts the click, fails to resolve it against `page_registry`, and
    renders its own 404 instead of a real navigation (a browser refresh on the same URL works fine,
    since that bypasses the router entirely). See `dlboard/scripts/docker_deployment.py`'s sibling
    bug for `manage_url` in `app.py`, which already gets this right.
    """
    admin_user = store.update_user(
        store.get_or_create_user(models.Principal.unverified("admin")).model_copy(
            update={"scopes": [models.Scope.USER_MANAGE]}
        )
    )

    rendered = admin._render_users(store, admin_user, "/password/admin")

    add_user_link = cast("Any", rendered).children[0]  # pyrefly: ignore [unknown-variable-type]
    assert add_user_link.href == "/password/admin"
    assert add_user_link.refresh is True


def test_admin_layout_contains_tabs_and_purge_modal() -> None:
    layout = admin._admin_layout("trash")

    assert find_props(layout, admin.ADMIN_TABS_ID) is not None
    assert find_props(layout, admin.ADMIN_TRASH_CONTENT_ID) is not None
    assert find_props(layout, admin.ADMIN_AUDIT_LOG_CONTENT_ID) is not None
    purge_modal_props = find_props(layout, admin.ADMIN_PURGE_MODAL_ID)
    assert purge_modal_props is not None
    assert purge_modal_props["opened"] is False
