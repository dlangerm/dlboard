"""Retention policies and kept runs: what a project and its experiments are held to, and when, on every backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

import pendulum
import pytest

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, Clock, StoreBackend
from dlboard.models import RetentionUnit, RunAge, RunCount, Unlimited
from dlboard.serve._backend._retention_settings import RetentionSettings

if TYPE_CHECKING:
    from dlboard.serve import SQLStoreBase


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


class _World(NamedTuple):
    project_id: int
    experiment_id: int
    other_project_experiment_id: int
    admin: models.User


@pytest.fixture(autouse=True)
def _stopped(clock: Clock) -> None:
    """Every test here reads what it just saved: on a clock that only moves when it is told to."""


@pytest.fixture
def world(store: SQLStoreBase[Any], admin: models.User) -> _World:
    project_id, other_id = (store.create_project(models.NewProject(name=n, description="")).id for n in "ab")
    experiment_id, other_experiment = (
        store.create_experiment(models.NewExperiment(project_id=p)).id for p in (project_id, other_id)
    )
    return _World(project_id, experiment_id, other_experiment, admin)


_COUNT = RunCount(max_runs=10, unit=RetentionUnit.PROJECT)


def _rules(
    store: SQLStoreBase[Any], w: _World, experiment_id: int | None = None
) -> tuple[models.RetentionRule | None, models.RetentionRule | None]:
    retention = store.get_retention(w.project_id)
    scope = retention.project if experiment_id is None else retention.experiments.get(experiment_id)
    if scope is None:
        return None, None
    active, pending = scope.active, scope.pending
    return (active.rule if active else None, pending.rule if pending else None)


def test_a_new_project_is_held_to_the_server_default_at_once(
    store: SQLStoreBase[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLBOARD_RETENTION_DEFAULT_MAX_AGE_DAYS", "30")
    created = store.create_project(models.NewProject(name="p", description=""))
    scope = store.get_retention(created.id).project
    assert scope.active is not None
    assert (scope.active.rule, scope.active.effective_at, scope.pending) == (
        RunAge(max_age_days=30),
        created.created_at,
        None,
    )


def test_a_rule_waits_out_the_grace_period_while_the_one_in_effect_is_enforced(
    store: SQLStoreBase[Any], world: _World, clock: Clock
) -> None:
    store.set_retention_policy(world.project_id, None, _COUNT, world.admin)
    assert _rules(store, world) == (Unlimited(), _COUNT)
    clock.advance(days=RetentionSettings().retention_grace_days + 1)
    assert _rules(store, world) == (_COUNT, None)


def test_saving_again_replaces_the_pending_rule_and_saving_the_active_one_cancels_it(
    store: SQLStoreBase[Any], world: _World, clock: Clock
) -> None:
    store.set_retention_policy(world.project_id, None, _COUNT, world.admin)
    clock.advance(days=RetentionSettings().retention_grace_days + 1)
    assert _rules(store, world) == (_COUNT, None)
    store.set_retention_policy(world.project_id, None, RunAge(max_age_days=5), world.admin)
    store.set_retention_policy(world.project_id, None, RunAge(max_age_days=9), world.admin)
    assert _rules(store, world) == (_COUNT, RunAge(max_age_days=9))
    store.set_retention_policy(world.project_id, None, _COUNT, world.admin)
    assert _rules(store, world) == (_COUNT, None)
    store.set_retention_policy(world.project_id, None, RunAge(max_age_days=9), world.admin)
    store.cancel_pending_retention(world.project_id, None, world.admin)
    assert _rules(store, world) == (_COUNT, None)
    policies = store.tables[models.RetentionPolicy]
    assert len(store._execute(policies.select().where(policies.c.project_id == world.project_id))) == 1


@pytest.mark.parametrize(
    ("project_rule", "experiment_rule", "allowed"),
    [
        (_COUNT, RunCount(max_runs=5, unit=RetentionUnit.PROJECT), True),
        (_COUNT, RunCount(max_runs=11, unit=RetentionUnit.PROJECT), False),
        (_COUNT, RunCount(max_runs=10, unit=RetentionUnit.EXPERIMENT), False),
        (RunCount(max_runs=10), RunCount(max_runs=10, unit=RetentionUnit.PROJECT), True),
        (_COUNT, RunAge(max_age_days=1), False),
        (RunAge(max_age_days=30), RunAge(max_age_days=7), True),
        (Unlimited(), RunAge(max_age_days=7), True),
        (_COUNT, Unlimited(), True),
    ],
)
def test_an_experiment_keeps_at_most_what_its_project_does_with_a_rule_of_its_kind(
    store: SQLStoreBase[Any],
    world: _World,
    project_rule: models.RetentionRule,
    experiment_rule: models.RetentionRule,
    *,
    allowed: bool,
) -> None:
    store.set_retention_policy(world.project_id, None, project_rule, world.admin)
    if allowed:
        store.set_retention_policy(world.project_id, world.experiment_id, experiment_rule, world.admin)
    else:
        with pytest.raises(ValueError, match="keeps at most"):
            store.set_retention_policy(world.project_id, world.experiment_id, experiment_rule, world.admin)


def test_an_experiment_of_another_project_has_no_rule_in_this_one(
    store: SQLStoreBase[Any], world: _World
) -> None:
    with pytest.raises(ValueError, match="is not an experiment of project"):
        store.set_retention_policy(world.project_id, world.other_project_experiment_id, _COUNT, world.admin)


def test_an_experiment_with_no_rule_of_its_own_that_saves_none_stores_nothing(
    store: SQLStoreBase[Any], world: _World
) -> None:
    store.set_retention_policy(world.project_id, world.experiment_id, Unlimited(), world.admin)
    assert store.get_retention(world.project_id).experiments == {}


@pytest.mark.parametrize(
    ("next_rule", "kept"),
    [(RunCount(max_runs=20, unit=RetentionUnit.PROJECT), True), (RunAge(max_age_days=5), False)],
    ids=["same kind", "another kind"],
)
def test_changing_the_kind_of_a_project_rule_drops_its_experiments_rules(
    store: SQLStoreBase[Any], world: _World, next_rule: models.RetentionRule, *, kept: bool
) -> None:
    store.set_retention_policy(world.project_id, None, _COUNT, world.admin)
    store.set_retention_policy(
        world.project_id, world.experiment_id, RunCount(max_runs=5, unit=RetentionUnit.PROJECT), world.admin
    )
    store.set_retention_policy(world.project_id, None, next_rule, world.admin)
    assert (world.experiment_id in store.get_retention(world.project_id).experiments) is kept


def test_a_project_s_policies_go_with_it_when_it_is_purged(store: SQLStoreBase[Any], world: _World) -> None:
    store.set_retention_policy(world.project_id, world.experiment_id, RunAge(max_age_days=1), world.admin)
    store.delete_project(world.project_id, world.admin)
    store.purge_project(world.project_id, world.admin)
    policies = store.tables[models.RetentionPolicy]
    assert store._execute(policies.select().where(policies.c.project_id == world.project_id)) == []


def test_a_kept_run_stays_kept_by_whoever_kept_it_until_it_is_released(
    store: SQLStoreBase[Any], world: _World
) -> None:
    alice = store.get_or_create_user(models.Principal.unverified("alice"))
    a, b, gone = (store.create_run(models.NewRun(experiment_id=world.experiment_id)).id for _ in range(3))
    store.keep_runs(frozenset({a}), alice)
    store.keep_runs(frozenset({a, b}), world.admin)
    assert store.kept_runs(world.experiment_id, run_ids=None) == {a, b}
    assert store.kept_runs(world.experiment_id, run_ids=frozenset({b, gone})) == {b}
    kept_a = store.get_run(a)
    assert kept_a is not None
    assert kept_a.kept_by == alice.id
    store.keep_runs(frozenset({gone}), alice)
    store.delete_run(gone, world.admin)
    assert store.kept_runs(world.experiment_id, run_ids=None) == {a, b}
    with pytest.raises(ValueError, match="has been deleted"):
        store.keep_runs(frozenset({gone}), alice)
    store.release_runs(frozenset({a}), world.admin)
    assert store.kept_runs(world.experiment_id, run_ids=None) == {b}


@pytest.mark.parametrize(
    ("env", "default", "error"),
    [
        ({}, Unlimited(), None),
        ({"RETENTION_DEFAULT_MAX_RUNS": "5"}, RunCount(max_runs=5), None),
        ({"RETENTION_MAX_AGE_DAYS": "90"}, RunAge(max_age_days=90), None),
        ({"RETENTION_DEFAULT_MAX_RUNS": "5", "RETENTION_DEFAULT_MAX_AGE_DAYS": "5"}, None, "not both"),
        ({"RETENTION_DEFAULT_MAX_RUNS": "50", "RETENTION_MAX_RUNS": "10"}, None, "keeps more than its cap"),
        (
            {"RETENTION_DEFAULT_MAX_RUNS": "5", "RETENTION_MAX_AGE_DAYS": "10"},
            None,
            "keeps more than its cap",
        ),
    ],
)
def test_the_default_rule_is_one_kind_within_the_caps(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    default: models.RetentionRule | None,
    error: str | None,
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(f"DLBOARD_{name}", value)
    if error is None:
        assert RetentionSettings().default_rule == default
    else:
        with pytest.raises(ValueError, match=error):
            RetentionSettings()


def test_only_one_holder_has_the_lease_until_it_runs_out(store: SQLStoreBase[Any], clock: Clock) -> None:
    until = pendulum.now("UTC").add(minutes=5)
    assert store.claim_retention_lease("a", until=until)
    assert not store.claim_retention_lease("b", until=until)
    assert store.claim_retention_lease("a", until=until.add(minutes=5))
    clock.advance(minutes=11)
    assert store.claim_retention_lease("b", until=pendulum.now("UTC").add(minutes=5))
    assert not store.claim_retention_lease("a", until=pendulum.now("UTC").add(minutes=5))


def test_a_project_is_out_of_spec_from_the_first_check_that_finds_runs_to_delete(
    store: SQLStoreBase[Any], world: _World, clock: Clock
) -> None:
    first = store.record_retention_status(world.project_id, overdue_runs=3)
    clock.advance(hours=1)
    later = store.record_retention_status(world.project_id, overdue_runs=1)
    assert (later.out_of_spec_since, later.overdue_runs) == (first.checked_at, 1)
    cleared = store.record_retention_status(world.project_id, overdue_runs=0)
    assert cleared.out_of_spec_since is None
    assert store.get_retention(world.project_id).status == cleared


def test_retention_has_run_since_its_first_sweep(store: SQLStoreBase[Any], clock: Clock) -> None:
    first = store.record_retention_sweep()
    clock.advance(days=3)
    assert store.record_retention_sweep() == first


def test_the_trash_is_listed_longest_in_it_first_up_to_a_cutoff(
    store: SQLStoreBase[Any], world: _World, clock: Clock
) -> None:
    runs = [store.create_run(models.NewRun(experiment_id=world.experiment_id)).id for _ in range(3)]
    for run_id in runs:
        store.delete_run(run_id, world.admin)
        clock.advance(days=1)
    cutoff = pendulum.now("UTC").subtract(hours=36)
    assert store.trashed_before(models.EntityType.RUN, cutoff, limit=10) == runs[:2]
    assert store.trashed_before(models.EntityType.RUN, cutoff, limit=1) == runs[:1]
    assert store.trashed_before(models.EntityType.EXPERIMENT, cutoff, limit=10) == []
