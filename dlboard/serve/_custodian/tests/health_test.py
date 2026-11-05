"""How background work stands, read back by a web process: `/healthz/custodian`, the admin tab, and who may read it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pendulum
import pytest

from dlboard import models
from dlboard._compat import StrEnum
from dlboard.conftest import dispose_stores
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.serve import app as build_app
from dlboard.serve._custodian import (
    NewWorkItem,
    Task,
    TaskOutcome,
    WorkRule,
    get_system_task_store,
    register_task,
)
from dlboard.serve._custodian._store import AuthorizingTaskStore
from dlboard.serve._pages._admin_tasks import render_background_tasks

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from dash import Dash
    from flask.testing import FlaskClient

    from dlboard.conftest import Clock
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


class _Name(StrEnum):
    SWEEP = "sweep"


_SWEEP = Task(name=_Name.SWEEP, run_once=lambda _ctx: None, every=pendulum.duration(minutes=5))


class _Sweeping:
    """A plugin with one task on an interval."""

    @classmethod
    def plug(cls, app: Dash) -> None:
        register_task(app, _SWEEP)


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Iterator[Dash]:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    built = build_app([sqlite, filesystem, *LOCAL_AUTH, *BUILTIN_BACKEND, _Sweeping])
    yield built
    dispose_stores(built)


def _health(client: FlaskClient) -> tuple[int, dict[str, Any]]:
    response = client.get("/healthz/custodian")
    return response.status_code, response.get_json()


def test_background_work_is_healthy_while_a_custodian_beats_and_no_task_is_overdue(
    app: Dash, clock: Clock
) -> None:
    store = get_system_task_store(app)
    client: FlaskClient = app.server.test_client()
    assert _health(client) == (503, {"status": "unavailable", "custodians": 0, "overdue": []})

    store.ensure_tasks([_Name.SWEEP])
    store.beat("c:1:x", host="c", pid=1, started_at=pendulum.now("UTC"))
    store.start_task_run(_Name.SWEEP)
    store.finish_task_run(_Name.SWEEP, TaskOutcome.SUCCESS, error=None, progress={"backlog": 0})
    assert _health(client) == (200, {"status": "ok", "custodians": 1, "overdue": []})

    clock.advance(minutes=16)  # three intervals and more without a success
    store.beat("c:1:x", host="c", pid=1, started_at=pendulum.now("UTC"))
    assert _health(client) == (503, {"status": "unavailable", "custodians": 1, "overdue": ["sweep"]})

    clock.advance(minutes=1)  # and no beat in three beats: no custodian
    assert _health(client)[1]["custodians"] == 0


def test_the_admin_tab_says_when_no_custodian_runs_and_which_task_is_overdue(app: Dash) -> None:
    store = get_system_task_store(app)
    assert "No custodian" in repr(render_background_tasks(store, [_SWEEP]))
    store.ensure_tasks([_Name.SWEEP])
    store.beat("c:1:x", host="c", pid=1, started_at=pendulum.now("UTC"))
    store.start_task_run(_Name.SWEEP)
    store.finish_task_run(_Name.SWEEP, TaskOutcome.HUNG, error="Still running", progress=None)
    shown = repr(render_background_tasks(store, [_SWEEP]))
    assert ("c:1:x" in shown, "overdue" in shown, "hung" in shown) == (True, True, True)


@pytest.mark.parametrize("enforce", [True, False])
def test_a_request_reads_background_work_as_an_admin_and_never_writes_it(
    store: SQLLiteStore, *, enforce: bool
) -> None:
    def user(name: str, *scopes: models.Scope) -> models.User:
        made = store.get_or_create_user(models.Principal(issuer="test", subject=name, username=name))
        return store.update_user(made.model_copy(update={"scopes": list(scopes)}))

    nobody, admin = user("nobody"), user("admin", models.Scope.RESTORE)
    as_nobody = AuthorizingTaskStore(store, nobody, enforce_grants=enforce)
    as_admin = AuthorizingTaskStore(store, admin, enforce_grants=enforce)
    assert as_admin.task_states() == []
    if enforce:
        with pytest.raises(PermissionError):
            as_nobody.task_states()
    else:
        assert as_nobody.task_states() == []
    writes: list[Callable[[AuthorizingTaskStore], object]] = [
        lambda s: s.request_task_run("sweep"),
        lambda s: s.ensure_tasks(["sweep"]),
        lambda s: s.renew_work("me", until=pendulum.now("UTC")),
        lambda s: s.claim_task("sweep", "me", until=pendulum.now("UTC")),
        lambda s: s.enqueue_work([NewWorkItem(kind="unruled", key="1")]),
    ]
    for write in writes:
        with pytest.raises(PermissionError):
            write(as_admin)


def test_work_a_request_queues_names_the_user_who_asked_whatever_it_says(store: SQLLiteStore) -> None:
    made = store.get_or_create_user(models.Principal(issuer="test", subject="admin", username="admin"))
    admin = store.update_user(made.model_copy(update={"scopes": [models.Scope.PURGE]}))
    rule = WorkRule(kind=_Name.SWEEP, scope=models.Scope.PURGE, task=_Name.SWEEP, actor_field="asked_by")
    tasks = AuthorizingTaskStore(store, admin, enforce_grants=False, rules={"sweep": rule})
    tasks.enqueue_work(
        [NewWorkItem(kind="sweep", key="1", payload={"asked_by": admin.id + 1, "other": "kept"})]
    )
    (item,) = store.claim_work("sweep", "me", limit=5, until=pendulum.now("UTC").add(minutes=1))
    assert item.payload == {"asked_by": admin.id, "other": "kept"}
