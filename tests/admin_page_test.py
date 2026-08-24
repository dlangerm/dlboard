# pyright: reportPrivateUsage=false
"""Tests for the admin page's pure rendering functions (trash list, audit log, layout)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from dltrack import models
from dltrack.models import constants
from dltrack.plugins.pages import simple_admin_page as admin
from tests.conftest import find_props

if TYPE_CHECKING:
    from dltrack.plugins.data_stores.sqlite import SQLLiteStore


def test_render_trash_is_empty_message_when_nothing_deleted(store: SQLLiteStore) -> None:
    rendered = admin._render_trash(store)

    assert cast("Any", rendered).children == "Nothing in the trash."


def test_render_trash_lists_a_deleted_project_with_restore_and_purge_buttons(
    store: SQLLiteStore,
) -> None:
    admin_user = store.get_or_create_user("admin")
    project = store.create_project(models.NewProject(name="p", description="d"))
    store.delete_project(project.id, actor_id=admin_user.id)

    rendered = admin._render_trash(store)

    restore_id = {
        "type": constants.ADMIN_RESTORE_BUTTON_TYPE,
        "entity_type": models.EntityType.PROJECT.value,
        "id": project.id,
    }
    purge_id = {
        "type": constants.ADMIN_PURGE_BUTTON_TYPE,
        "entity_type": models.EntityType.PROJECT.value,
        "id": project.id,
    }
    assert find_props(rendered, restore_id) is not None
    assert find_props(rendered, purge_id) is not None


def test_render_trash_caps_each_kind_and_notes_the_overflow(store: SQLLiteStore) -> None:
    """A single cascade can soft-delete thousands of artifacts; the trash must never render all of them."""
    admin_user = store.get_or_create_user("admin")
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
    for artifact in store.fetch_artifacts(experiment_id=experiment.id):
        assert artifact.id is not None
        store.delete_artifact(artifact.id, actor_id=admin_user.id)

    rendered = cast("Any", admin._render_trash(store))

    # only the Artifact kind has any deleted items here; children are [heading, *rows, overflow note]
    (artifact_section,) = rendered.children
    heading, *rest = artifact_section.children
    *rows, overflow_note = rest
    assert "Artifacts" in heading.children
    assert len(rows) == admin._TRASH_PAGE_SIZE_PER_KIND
    assert "most recently deleted" in overflow_note.children


def test_render_trash_omits_a_project_that_is_not_deleted(store: SQLLiteStore) -> None:
    store.create_project(models.NewProject(name="p", description="d"))

    rendered = admin._render_trash(store)

    assert cast("Any", rendered).children == "Nothing in the trash."


def test_render_audit_log_is_empty_message_with_no_entries(store: SQLLiteStore) -> None:
    rendered = admin._render_audit_log(store)

    assert cast("Any", rendered).children == "No audit log entries yet."


def test_render_audit_log_lists_a_recorded_action(store: SQLLiteStore) -> None:
    admin_user = store.get_or_create_user("admin")
    project = store.create_project(models.NewProject(name="p", description="d"))
    store.delete_project(project.id, actor_id=admin_user.id)

    rendered = cast("Any", admin._render_audit_log(store))

    # one row in the table body (`rendered.children` is [Thead, Tbody])
    body_rows = rendered.children[1].children
    assert len(body_rows) == 1
    action_cell = body_rows[0].children[2]
    assert action_cell.children == models.AuditAction.SOFT_DELETE.value


def test_admin_layout_contains_tabs_and_purge_modal() -> None:
    layout = admin._admin_layout()

    assert find_props(layout, constants.ADMIN_TABS_ID) is not None
    assert find_props(layout, constants.ADMIN_TRASH_CONTENT_ID) is not None
    assert find_props(layout, constants.ADMIN_AUDIT_LOG_CONTENT_ID) is not None
    purge_modal_props = find_props(layout, constants.ADMIN_PURGE_MODAL_ID)
    assert purge_modal_props is not None
    assert purge_modal_props["opened"] is False
