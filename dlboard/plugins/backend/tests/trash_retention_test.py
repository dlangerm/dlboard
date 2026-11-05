"""The Trash empties itself: purged once in it long enough, counting from the first run, as the Trash's own user."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dlboard import models
from dlboard.conftest import create_entity_chain, dispose_stores, run_task
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dlboard.plugins.backend import artifact_purge_worker, trash_retention
from dlboard.plugins.backend.artifact_purge_worker import PurgeTask
from dlboard.plugins.backend.trash_retention import TRASH_PRINCIPAL, TrashTask
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.serve import SQLStoreBase, get_system_data_store
from dlboard.serve import app as build_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from dash import Dash

    from dlboard.conftest import Clock


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Iterator[Dash]:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    built = build_app(
        [sqlite, filesystem, artifact_purge_worker, trash_retention, *LOCAL_AUTH, *BUILTIN_BACKEND]
    )
    yield built
    dispose_stores(built)


def test_what_has_been_in_the_trash_long_enough_is_purged_counting_from_the_first_run(
    app: Dash, clock: Clock
) -> None:
    store = get_system_data_store(app)
    assert isinstance(store, SQLStoreBase)
    admin = store.get_or_create_user(models.Principal.unverified("admin"))
    _, _, before, _ = create_entity_chain(store)
    store.delete_run(before, admin)
    clock.advance(days=40)

    run_task(app, TrashTask.RETENTION)  # the first: what was deleted before it waits its 30 days too
    run_task(app, PurgeTask.ENTITIES)
    assert [r.id for r in store.list_deleted_runs()] == [before]

    clock.advance(days=29)
    _, _, after, _ = create_entity_chain(store)
    store.delete_run(after, admin)
    clock.advance(days=2)
    run_task(app, TrashTask.RETENTION)
    run_task(app, PurgeTask.ENTITIES)

    assert [r.id for r in store.list_deleted_runs()] == [after]  # in it 2 days: kept
    trash_user = store.get_or_create_user(TRASH_PRINCIPAL)
    purges = [e for e in store.list_audit_log(admin) if e.action is models.AuditAction.PURGE]
    assert [(e.entity_id, e.user_id) for e in purges] == [(before, trash_user.id)]
