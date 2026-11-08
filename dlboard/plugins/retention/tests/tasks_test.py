"""Retention's tasks: what a check deletes and as whom, how far a budget takes it, and what the schedule queues."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pendulum
import pytest

from dlboard import models
from dlboard.conftest import dispose_stores, recorded_statements, run_task, scraped_total
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dlboard.plugins import retention as retention_plugin
from dlboard.plugins.backend import artifact_purge_worker
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.plugins.retention._candidates import Candidates
from dlboard.plugins.retention._rules import (
    RetentionKey,
    RetentionRule,
    RetentionSetting,
    RetentionStatus,
    RuleVersion,
    RunCount,
)
from dlboard.plugins.retention._tasks import RETENTION_PRINCIPAL, RetentionTask, RetentionWork
from dlboard.serve import SQLStoreBase, get_system_data_store
from dlboard.serve import app as build_app
from dlboard.serve._custodian import get_system_task_store
from dlboard.serve.telemetry._prometheus import serve_metrics

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from dash import Dash

    from dlboard.conftest import Clock


@pytest.fixture(autouse=True)
def _eager(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    """A rule takes effect when saved and acts on the first run past it; batches of two."""
    for name, value in {
        "RETENTION_GRACE_DAYS": "0",
        "RETENTION_RUN_SLACK": "0",
        "RETENTION_BATCH": "2",
    }.items():
        monkeypatch.setenv(f"DLBOARD_{name}", value)


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Dash]:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    built = build_app(
        [sqlite, filesystem, artifact_purge_worker, retention_plugin, *LOCAL_AUTH, *BUILTIN_BACKEND]
    )
    yield built
    dispose_stores(built)


@pytest.fixture
def store(app: Dash) -> SQLStoreBase[Any]:
    found = get_system_data_store(app)
    assert isinstance(found, SQLStoreBase)
    return found


@pytest.fixture
def admin(store: SQLStoreBase[Any]) -> models.User:
    return store.get_or_create_user(models.Principal.unverified("admin"))


def _project(
    store: SQLStoreBase[Any], admin: models.User, runs: int, rule: RetentionRule
) -> tuple[int, list[int]]:
    """A project held to `rule` (already in effect), with an experiment of `runs` finished runs, oldest first."""
    project_id = store.create_project(models.NewProject(name=f"p{runs}", description="")).id
    experiment_id = store.create_experiment(models.NewExperiment(project_id=project_id)).id
    start = pendulum.now("UTC").subtract(hours=1)
    run_ids = [
        store.create_run(
            models.NewRun(
                experiment_id=experiment_id, status=models.RunStatus.FINISHED, created_at=start.add(seconds=i)
            )
        ).id
        for i in range(runs)
    ]
    now = pendulum.now("UTC")
    setting = RetentionSetting().saved(rule, by=admin.id, now=now, grace=pendulum.duration())
    store.put_scoped_setting(RetentionKey.RULE, project_id, None, setting.dump(), admin)
    return project_id, run_ids


def _live(store: SQLStoreBase[Any], run_ids: list[int]) -> list[int]:
    return [run_id for run_id in run_ids if store.get_run(run_id) is not None]


def _status(store: SQLStoreBase[Any], project_id: int) -> RetentionStatus:
    found = store.get_scoped_settings(RetentionKey.STATUS, project_id).project
    assert found is not None
    return RetentionStatus.model_validate(found.value)


def test_a_check_moves_what_a_rule_says_to_delete_to_the_trash_as_the_retention_user(
    app: Dash, store: SQLStoreBase[Any], admin: models.User
) -> None:
    project_id, runs = _project(store, admin, 5, RunCount(max_runs=2))
    run_task(app, RetentionTask.SCHEDULE)
    run_task(app, RetentionTask.CHECK)

    assert _live(store, runs) == runs[3:]
    retention = store.get_or_create_user(RETENTION_PRINCIPAL)
    deletions = [e for e in store.list_audit_log(admin) if e.action is models.AuditAction.SOFT_DELETE]
    assert {(e.entity_id, e.user_id) for e in deletions} == {(run, retention.id) for run in runs[:3]}
    status = _status(store, project_id)
    assert (status.overdue_runs, status.out_of_spec_since) == (0, None)


def test_a_check_past_its_budget_leaves_the_rest_for_the_next_and_says_the_project_is_out_of_spec(
    app: Dash, store: SQLStoreBase[Any], admin: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLBOARD_RETENTION_PROJECT_BUDGET", "2")
    project_id, runs = _project(store, admin, 5, RunCount(max_runs=2))
    for _ in range(2):
        run_task(app, RetentionTask.SCHEDULE)
        run_task(app, RetentionTask.CHECK)
        status = _status(store, project_id)
        if _live(store, runs) == runs[2:]:
            assert status.overdue_runs == 1
            assert status.out_of_spec_since is not None
    assert _live(store, runs) == runs[3:]


def test_the_schedule_queues_a_check_of_each_project_with_a_rule_once_however_often_it_runs(
    app: Dash, store: SQLStoreBase[Any], admin: models.User
) -> None:
    _project(store, admin, 3, RunCount(max_runs=2))
    store.create_project(models.NewProject(name="unruled", description=""))
    tasks = get_system_task_store(app)
    for _ in range(3):
        run_task(app, RetentionTask.SCHEDULE)
    assert tasks.count_work(RetentionWork.CHECK) == 1


def test_a_project_past_a_lowered_cap_is_given_the_default_after_the_grace_period_unless_an_admin_set_it(
    app: Dash, store: SQLStoreBase[Any], admin: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    capped, _ = _project(store, admin, 1, RunCount(max_runs=50))
    exempt, _ = _project(store, admin, 2, RunCount(max_runs=50))
    now = pendulum.now("UTC")
    exempt_version = RuleVersion(rule=RunCount(max_runs=50), set_at=now, effective_at=now, cap_exempt=True)
    store.put_scoped_setting(
        RetentionKey.RULE, exempt, None, RetentionSetting(active=exempt_version).dump(), admin
    )
    monkeypatch.setenv("DLBOARD_RETENTION_GRACE_DAYS", "7")
    monkeypatch.setenv("DLBOARD_RETENTION_MAX_RUNS", "10")

    run_task(app, RetentionTask.SCHEDULE)

    def pending(project_id: int) -> RetentionRule | None:
        found = store.get_scoped_settings(RetentionKey.RULE, project_id).project
        assert found is not None
        latest = RetentionSetting.model_validate(found.value).pending
        return latest.rule if latest else None

    assert (pending(capped), pending(exempt)) == (RunCount(max_runs=10), None)


def test_a_check_counts_the_runs_it_moved_to_the_trash(
    app: Dash, store: SQLStoreBase[Any], admin: models.User, telemetry: pytest.MonkeyPatch
) -> None:
    telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "prometheus")
    _project(store, admin, 5, RunCount(max_runs=2))
    run_task(app, RetentionTask.SCHEDULE)
    run_task(app, RetentionTask.CHECK)
    assert scraped_total(serve_metrics().get_data(as_text=True), "dlboard_retention_runs_deleted_total") == 3


def test_a_run_kept_after_a_check_found_it_is_not_deleted(
    app: Dash, store: SQLStoreBase[Any], admin: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The statement that deletes decides: a read of the kept flag before it would be as stale as the check's own."""
    _, runs = _project(store, admin, 5, RunCount(max_runs=2))
    read = Candidates.read
    kept: list[int] = []

    def find_then_keep(self: Candidates, *args: Any, **kwargs: Any) -> list[int]:  # noqa: ANN401
        found = read(self, *args, **kwargs)
        if found and not kept:
            kept.append(found[0])
            store.keep_runs(frozenset({found[0]}), admin)
        return found

    monkeypatch.setattr(Candidates, "read", find_then_keep)
    run_task(app, RetentionTask.SCHEDULE)
    run_task(app, RetentionTask.CHECK)
    assert runs[0] in _live(store, runs)
    assert _live(store, runs) == [runs[0], *runs[3:]]


def test_scheduling_costs_no_statement_for_a_project_with_no_rule_and_no_default(
    app: Dash, store: SQLStoreBase[Any], admin: models.User
) -> None:
    _project(store, admin, 3, RunCount(max_runs=2))
    for i in range(30):
        store.create_project(models.NewProject(name=f"idle{i}", description=""))
    with recorded_statements(store._engine) as statements:
        run_task(app, RetentionTask.SCHEDULE)
    assert len(statements) < 15, statements  # the ruled project's own, and the sweep's: not thirty more


def test_a_check_works_out_where_to_cut_once_however_many_batches_it_deletes_in(
    app: Dash, store: SQLStoreBase[Any], admin: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(store, admin, 9, RunCount(max_runs=1))  # eight runs to delete, two a batch
    cuts: list[int] = []
    cutoffs = Candidates.cutoffs

    def counting(self: Candidates, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        cuts.append(1)
        return cutoffs(self, *args, **kwargs)

    monkeypatch.setattr(Candidates, "cutoffs", counting)
    run_task(app, RetentionTask.SCHEDULE)
    run_task(app, RetentionTask.CHECK)
    assert len(cuts) == 1
