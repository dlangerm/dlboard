"""Which runs a project's retention rules say to delete: past the slack, down to the rule, never a kept one, on every backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

import pendulum
import pytest

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, Clock, StoreBackend
from dlboard.models import RetentionUnit, RunAge, RunCount, RunStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from dlboard.serve import SQLStoreBase


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


@pytest.fixture(autouse=True)
def _eager(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    """A rule takes effect when saved; a count acts two runs past itself, an age a day past itself."""
    monkeypatch.setenv("DLBOARD_RETENTION_GRACE_DAYS", "0")
    monkeypatch.setenv("DLBOARD_RETENTION_RUN_SLACK", "2")
    monkeypatch.setenv("DLBOARD_RETENTION_AGE_SLACK_DAYS", "1")


class _Project(NamedTuple):
    id: int
    experiments: tuple[int, int]
    log: Callable[..., list[int]]
    """`log(experiment_index, count, days_ago=..., status=...)`: runs, oldest first, a second apart."""


@pytest.fixture
def project(store: SQLStoreBase[Any]) -> _Project:
    project_id = store.create_project(models.NewProject(name="p", description="")).id
    first, second = (
        store.create_experiment(models.NewExperiment(project_id=project_id)).id for _ in range(2)
    )
    clock = iter(range(10_000, 0, -1))

    def log(
        experiment: int, count: int, *, days_ago: float = 0, status: RunStatus = RunStatus.FINISHED
    ) -> list[int]:
        start = pendulum.now("UTC") - pendulum.duration(days=days_ago)
        return [
            store.create_run(
                models.NewRun(
                    experiment_id=(first, second)[experiment],
                    status=status,
                    created_at=start.subtract(seconds=next(clock)),
                )
            ).id
            for _ in range(count)
        ]

    return _Project(project_id, (first, second), log)


def _candidates(
    store: SQLStoreBase[Any], project: _Project, rule: models.RetentionRule, admin: models.User
) -> list[int]:
    store.set_retention_policy(project.id, None, rule, admin)
    found = store.retention_candidates(project.id, limit=100)
    assert store.retention_preview(project.id, None, rule, limit=100) == len(found)
    return found


@pytest.mark.parametrize(("logged", "deleted"), [(5, 0), (6, 3)], ids=["within the slack", "past it"])
def test_a_count_of_each_experiment_acts_past_its_slack_and_deletes_down_to_the_count(
    store: SQLStoreBase[Any], project: _Project, admin: models.User, logged: int, deleted: int
) -> None:
    runs = project.log(0, logged)
    project.log(1, 3)
    assert (
        _candidates(store, project, RunCount(max_runs=3, unit=RetentionUnit.EXPERIMENT), admin)
        == runs[:deleted]
    )


def test_a_count_of_the_project_keeps_its_newest_runs_whichever_experiment_they_are_of(
    store: SQLStoreBase[Any], project: _Project, admin: models.User
) -> None:
    older, newer = project.log(0, 3), project.log(1, 3)
    assert _candidates(store, project, RunCount(max_runs=3, unit=RetentionUnit.PROJECT), admin) == older
    assert newer


def test_a_kept_run_is_neither_counted_nor_deleted(
    store: SQLStoreBase[Any], project: _Project, admin: models.User
) -> None:
    runs = project.log(0, 7)
    store.keep_runs(frozenset(runs[:2]), admin)
    assert _candidates(store, project, RunCount(max_runs=2), admin) == runs[2:5]
    store.keep_runs(frozenset(runs[2:3]), admin)
    assert store.retention_candidates(project.id, limit=100) == []


@pytest.mark.parametrize(("days_ago", "deleted"), [(0, False), (8, True)], ids=["running", "abandoned"])
def test_a_run_still_running_is_counted_but_not_deleted_until_its_grace_is_over(
    store: SQLStoreBase[Any], project: _Project, admin: models.User, days_ago: int, *, deleted: bool
) -> None:
    (running,) = project.log(0, 1, days_ago=days_ago, status=RunStatus.RUNNING)
    rest = project.log(0, 5)
    found = _candidates(store, project, RunCount(max_runs=3), admin)
    assert found == ([running] if deleted else []) + rest[:2]


@pytest.mark.parametrize(
    ("ages", "deleted"), [((3, 6, 7), [7, 6]), ((3, 5.5), [])], ids=["past the slack", "within it"]
)
def test_an_age_acts_past_its_slack_and_deletes_every_run_older_than_it(
    store: SQLStoreBase[Any],
    project: _Project,
    admin: models.User,
    ages: tuple[float, ...],
    deleted: list[float],
) -> None:
    runs = {age: project.log(0, 1, days_ago=age)[0] for age in ages}
    assert _candidates(store, project, RunAge(max_age_days=5), admin) == [runs[age] for age in deleted]


def test_an_experiment_held_to_a_stricter_rule_deletes_down_to_its_own(
    store: SQLStoreBase[Any], project: _Project, admin: models.User
) -> None:
    strict, lax = project.log(0, 6), project.log(1, 6)
    store.set_retention_policy(project.id, None, RunCount(max_runs=10), admin)
    store.set_retention_policy(project.id, project.experiments[0], RunCount(max_runs=3), admin)
    assert store.retention_candidates(project.id, limit=100) == strict[:3]
    assert store.retention_preview(project.id, project.experiments[1], RunCount(max_runs=3), limit=100) == 6
    assert lax


def test_candidates_are_the_oldest_first_at_most_the_limit_and_never_of_a_deleted_experiment(
    store: SQLStoreBase[Any], project: _Project, admin: models.User
) -> None:
    first, second = project.log(0, 6), project.log(1, 6)
    store.set_retention_policy(project.id, None, RunCount(max_runs=1), admin)
    assert store.retention_candidates(project.id, limit=4) == first[:4]
    store.delete_experiment(project.experiments[0], admin)
    assert store.retention_candidates(project.id, limit=100) == second[:5]
