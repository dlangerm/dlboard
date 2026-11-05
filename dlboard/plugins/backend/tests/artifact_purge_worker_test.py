"""The purge tasks: entities purged as whoever asked, every blob they orphan deleted, and a failure tried again later."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from dlboard import models
from dlboard.conftest import create_entity_chain, dispose_stores, run_task, scraped_total
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dlboard.plugins.backend import artifact_purge_worker
from dlboard.plugins.backend.artifact_purge_worker import PurgeTask
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.serve import app as build_app
from dlboard.serve import get_system_artifact_store, get_system_data_store
from dlboard.serve._backend._purge_work import PurgeWork, entity_purge
from dlboard.serve._custodian import get_system_task_store
from dlboard.serve._custodian._store import WORK_RULES, AuthorizingTaskStore
from dlboard.serve.telemetry._prometheus import serve_metrics

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from dash import Dash
    from pydantic import AnyUrl

    from dlboard.conftest import Clock
    from dlboard.serve import SQLStoreBase


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Iterator[Dash]:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    built = build_app([sqlite, filesystem, artifact_purge_worker, *LOCAL_AUTH, *BUILTIN_BACKEND])
    yield built
    dispose_stores(built)


def _store(app: Dash) -> SQLStoreBase[Any]:
    store = get_system_data_store(app)
    assert hasattr(store, "tables")
    return store  # pyrefly: ignore [bad-return]


def _purged_project_of(store: SQLStoreBase[Any], admin: models.User, count: int) -> list[str]:
    """Soft-delete then purge a project of `count` artifacts: the refs of the blobs it orphaned."""
    project_id, experiment_id, run_id, _ = create_entity_chain(store)
    refs = [f"file:///a{i}.png" for i in range(count)]
    store.log_artifact_refs(
        models.Artifact(
            key=f"a{i}", fname="a.png", run_id=run_id, experiment_id=experiment_id, step=i, ref=ref
        )
        for i, ref in enumerate(refs)
    )
    store.delete_project(project_id, admin)
    store.purge_project(project_id, admin)
    return refs


def test_every_orphaned_blob_is_deleted_and_one_that_fails_is_tried_again_later(
    app: Dash, clock: Clock, monkeypatch: pytest.MonkeyPatch, telemetry: pytest.MonkeyPatch
) -> None:
    telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "prometheus")
    store, tasks = _store(app), get_system_task_store(app)
    admin = store.get_or_create_user(models.Principal.unverified("admin"))
    refs = _purged_project_of(store, admin, 3)
    deleted: list[str] = []

    def delete(ref: AnyUrl) -> None:
        if str(ref) == refs[1] and len(deleted) < 2:
            msg = "the object store is down"
            raise ConnectionError(msg)
        deleted.append(str(ref))

    monkeypatch.setattr(get_system_artifact_store(app), "delete_artifact", delete)
    run_task(app, PurgeTask.BLOBS)
    assert (sorted(deleted), tasks.count_work(PurgeWork.BLOB)) == (sorted([refs[0], refs[2]]), 1)
    (state,) = [s for s in tasks.task_states() if s.name == PurgeTask.BLOBS]
    assert state.progress["backlog"] == 1

    run_task(app, PurgeTask.BLOBS)  # not due yet: it backs off
    assert tasks.count_work(PurgeWork.BLOB) == 1
    clock.advance(minutes=2)
    run_task(app, PurgeTask.BLOBS)
    assert (sorted(deleted), tasks.count_work(PurgeWork.BLOB)) == (sorted(refs), 0)
    scrape = serve_metrics().get_data(as_text=True)
    assert scraped_total(scrape, "dlboard_artifact_purges_total", dlboard_outcome="success") == 3
    assert scraped_total(scrape, "dlboard_artifact_purges_total", dlboard_outcome="failure") == 1


def test_an_entity_purge_is_done_as_whoever_asked_and_asks_for_its_blobs_at_once(app: Dash) -> None:
    store, tasks = _store(app), get_system_task_store(app)
    admin = store.get_or_create_user(models.Principal.unverified("admin"))
    project_id, *_ = create_entity_chain(store, artifact=True)
    store.delete_project(project_id, admin)
    tasks.enqueue_work([entity_purge(models.EntityType.PROJECT, project_id, requested_by=admin.id)])

    run_task(app, PurgeTask.ENTITIES)

    assert list(store.list_deleted_projects()) == []
    assert [e.user_id for e in store.list_audit_log(admin) if e.action is models.AuditAction.PURGE] == [
        admin.id
    ]
    assert (tasks.count_work(PurgeWork.ENTITY), tasks.count_work(PurgeWork.BLOB)) == (0, 1)
    (blobs,) = [s for s in tasks.task_states() if s.name == PurgeTask.BLOBS]
    assert blobs.requested_at is not None


def test_what_was_restored_since_its_purge_was_asked_for_is_left_alone(app: Dash) -> None:
    store, tasks = _store(app), get_system_task_store(app)
    admin = store.get_or_create_user(models.Principal.unverified("admin"))
    project_id, *_ = create_entity_chain(store)
    store.delete_project(project_id, admin)
    tasks.enqueue_work([entity_purge(models.EntityType.PROJECT, project_id, requested_by=admin.id)])
    store.restore_project(project_id, admin)

    run_task(app, PurgeTask.ENTITIES)

    assert store.get_project(project_id) is not None
    assert tasks.count_work(PurgeWork.ENTITY) == 0


@pytest.mark.parametrize("scopes", [[models.Scope.PURGE], []], ids=["purger", "anyone else"])
def test_a_request_queues_a_purge_only_as_someone_who_may_purge(
    app: Dash, scopes: list[models.Scope]
) -> None:
    store, tasks = _store(app), get_system_task_store(app)
    user = store.get_or_create_user(models.Principal(issuer="test", subject="u", username="u"))
    user = store.update_user(user.model_copy(update={"scopes": scopes}))
    as_user = AuthorizingTaskStore(tasks, user, enforce_grants=True, rules=WORK_RULES.find(app))
    asked = entity_purge(models.EntityType.RUN, 1, requested_by=user.id)
    if not scopes:
        with pytest.raises(PermissionError):
            as_user.enqueue_work([asked])
        return
    as_user.enqueue_work([asked])
    assert tasks.count_work(PurgeWork.ENTITY) == 1
    (state,) = [s for s in tasks.task_states() if s.name == PurgeTask.ENTITIES]
    assert state.requested_at is not None
    with pytest.raises(PermissionError):  # only the kinds a plugin lets a request queue
        as_user.enqueue_work([asked.model_copy(update={"kind": PurgeWork.BLOB})])
