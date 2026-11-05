"""A retention sweep: what it deletes, as whom, how far a budget takes it, and when it empties the Trash."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, cast

import pendulum
import pytest

from dlboard import models
from dlboard.conftest import scraped_total
from dlboard.plugins.backend import retention_worker
from dlboard.plugins.backend.retention_worker import RETENTION_PRINCIPAL, Sweeper
from dlboard.serve._backend._app_state import AppState
from dlboard.serve.telemetry._prometheus import serve_metrics

if TYPE_CHECKING:
    from dash import Dash

    from dlboard.conftest import Clock
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


@pytest.fixture(autouse=True)
def _eager(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    """A rule takes effect when saved and acts on the first run past it; batches of two, never a pause."""
    for name, value in {
        "RETENTION_GRACE_DAYS": "0",
        "RETENTION_RUN_SLACK": "0",
        "RETENTION_BATCH": "2",
    }.items():
        monkeypatch.setenv(f"DLBOARD_{name}", value)


def _sweeper(store: SQLLiteStore, holder: str = "me") -> Sweeper:
    return Sweeper(store, holder, pause=lambda _seconds: None)


def _project(
    store: SQLLiteStore, admin: models.User, runs: int, rule: models.RetentionRule
) -> tuple[int, list[int]]:
    """A project held to `rule`, with an experiment of `runs` finished runs, oldest first."""
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
    store.set_retention_policy(project_id, None, rule, admin)
    return project_id, run_ids


def _live(store: SQLLiteStore, run_ids: list[int]) -> list[int]:
    return [run_id for run_id in run_ids if store.get_run(run_id) is not None]


def test_a_sweep_moves_what_a_policy_says_to_delete_to_the_trash_as_the_retention_user(
    store: SQLLiteStore, admin: models.User
) -> None:
    project_id, runs = _project(store, admin, 5, models.RunCount(max_runs=2))
    _sweeper(store).sweep()

    assert _live(store, runs) == runs[3:]
    retention = store.get_or_create_user(RETENTION_PRINCIPAL)
    deletions = [e for e in store.list_audit_log(admin) if e.action is models.AuditAction.SOFT_DELETE]
    assert {(e.entity_id, e.user_id) for e in deletions} == {(run, retention.id) for run in runs[:3]}
    status = store.get_retention(project_id).status
    assert status is not None
    assert (status.overdue_runs, status.out_of_spec_since) == (0, None)


def test_a_sweep_past_its_budget_leaves_the_rest_for_the_next_and_says_the_project_is_out_of_spec(
    store: SQLLiteStore, admin: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLBOARD_RETENTION_SWEEP_BUDGET", "2")
    project_id, runs = _project(store, admin, 5, models.RunCount(max_runs=2))
    sweeper = _sweeper(store)

    sweeper.sweep()
    status = store.get_retention(project_id).status
    assert status is not None
    assert (_live(store, runs), status.overdue_runs) == (runs[2:], 1)
    assert status.out_of_spec_since is not None
    sweeper.sweep()
    assert _live(store, runs) == runs[3:]


def test_only_the_process_holding_the_lease_sweeps(store: SQLLiteStore, admin: models.User) -> None:
    _, runs = _project(store, admin, 5, models.RunCount(max_runs=2))
    assert store.claim_retention_lease("another", until=pendulum.now("UTC").add(hours=1))
    _sweeper(store).sweep()
    assert _live(store, runs) == runs


def test_a_project_from_before_retention_or_past_a_lowered_cap_is_given_the_default_after_the_grace_period(
    store: SQLLiteStore, admin: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, _ = _project(store, admin, 0, models.RunAge(max_age_days=90))
    capped, _ = _project(store, admin, 1, models.RunCount(max_runs=50))
    exempt, _ = _project(store, admin, 2, models.RunCount(max_runs=50))
    store.set_retention_policy(exempt, None, models.RunCount(max_runs=60), admin, cap_exempt=True)
    policies = store.tables[models.RetentionPolicy]
    store._execute(policies.delete().where(policies.c.project_id == old))
    monkeypatch.setenv("DLBOARD_RETENTION_GRACE_DAYS", "7")
    monkeypatch.setenv("DLBOARD_RETENTION_MAX_RUNS", "10")

    _sweeper(store).sweep()

    def pending(project_id: int) -> models.RetentionRule | None:
        scope = store.get_retention(project_id).project
        return scope.pending.rule if scope.pending else None

    assert (pending(old), pending(capped), pending(exempt)) == (
        models.RunCount(max_runs=10),
        models.RunCount(max_runs=10),
        None,
    )


def test_the_trash_is_emptied_of_what_has_been_in_it_long_enough_counted_from_the_first_sweep(
    store: SQLLiteStore, admin: models.User, clock: Clock
) -> None:
    _, (before, after) = _project(store, admin, 2, models.Unlimited())
    store.delete_run(before, admin)
    clock.advance(days=40)
    sweeper = _sweeper(store)
    sweeper.sweep()  # the first: what was in the Trash before it is not purged at once
    store.delete_run(after, admin)
    assert store.trashed_before(models.EntityType.RUN, pendulum.now("UTC").add(days=1), limit=10) == [
        before,
        after,
    ]

    clock.advance(days=31)
    sweeper.sweep()
    assert store.trashed_before(models.EntityType.RUN, pendulum.now("UTC").add(days=1), limit=10) == []
    purges = [e.entity_id for e in store.list_audit_log(admin) if e.action is models.AuditAction.PURGE]
    assert sorted(purges) == [before, after]


@pytest.mark.parametrize("enabled", [True, False])
def test_the_worker_starts_only_where_retention_is_enabled(
    monkeypatch: pytest.MonkeyPatch, *, enabled: bool
) -> None:
    monkeypatch.setenv("DLBOARD_RETENTION_ENABLED", str(enabled).lower())
    started = threading.Event()

    def run(_app: Dash) -> None:
        started.set()

    monkeypatch.setattr(retention_worker, "_run", run)
    retention_worker.plug(cast("Dash", object()))
    assert started.wait(5 if enabled else 0.2) is enabled


def test_a_sweep_counts_what_it_deleted_and_what_is_left_and_takes_its_count_back_when_it_loses_the_lease(
    store: SQLLiteStore, admin: models.User, telemetry: pytest.MonkeyPatch
) -> None:
    telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "prometheus")
    telemetry.setenv("DLBOARD_RETENTION_SWEEP_BUDGET", "2")
    _project(store, admin, 5, models.RunCount(max_runs=2))
    sweeper = _sweeper(store)
    sweeper.sweep()

    def scraped(name: str) -> float:
        return scraped_total(serve_metrics().get_data(as_text=True), name)

    assert (scraped("dlboard_retention_runs_deleted_total"), scraped("dlboard_retention_runs_overdue")) == (
        2,
        1,
    )
    assert scraped("dlboard_retention_sweep_duration_seconds_count") == 1
    store._execute(store.tables[AppState].update().values(retention_lease_holder="another"))
    sweeper.sweep()
    assert scraped("dlboard_retention_runs_overdue") == 0
