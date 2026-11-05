"""Which runs a project's retention rules say to delete: past the slack, down to the rule, never a kept one, on every backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

import pendulum
import pytest

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, Clock, StoreBackend
from dlboard.models import RunStatus
from dlboard.plugins.retention._candidates import Candidates, Rules
from dlboard.plugins.retention._rules import RetentionRule, RetentionUnit, RunAge, RunCount, Unlimited
from dlboard.plugins.retention._settings import RetentionSettings

if TYPE_CHECKING:
    from collections.abc import Callable

    from dlboard.serve import SQLStoreBase


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


@pytest.fixture(autouse=True)
def _slack(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    """A count acts two runs past itself, an age a day past itself."""
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
    seconds = iter(range(10_000, 0, -1))

    def log(
        experiment: int, count: int, *, days_ago: float = 0, status: RunStatus = RunStatus.FINISHED
    ) -> list[int]:
        start = pendulum.now("UTC") - pendulum.duration(days=days_ago)
        return [
            store.create_run(
                models.NewRun(
                    experiment_id=(first, second)[experiment],
                    status=status,
                    created_at=start.subtract(seconds=next(seconds)),
                )
            ).id
            for _ in range(count)
        ]

    return _Project(project_id, (first, second), log)


def _find(
    store: SQLStoreBase[Any], project: _Project, rule: RetentionRule, **experiments: RetentionRule
) -> list[int]:
    """What `rule` says to delete, with the experiments' own rules by index (`e0=`, `e1=`)."""
    own = {project.experiments[int(name[1:])]: r for name, r in experiments.items()}
    return Candidates(store, RetentionSettings(), pendulum.now("UTC")).find(
        project.id, Rules(project=rule, experiments=own), limit=100
    )


@pytest.mark.parametrize(("logged", "deleted"), [(5, 0), (6, 3)], ids=["within the slack", "past it"])
def test_a_count_of_each_experiment_acts_past_its_slack_and_deletes_down_to_the_count(
    store: SQLStoreBase[Any], project: _Project, logged: int, deleted: int
) -> None:
    runs = project.log(0, logged)
    project.log(1, 3)
    assert _find(store, project, RunCount(max_runs=3, unit=RetentionUnit.EXPERIMENT)) == runs[:deleted]


def test_a_count_of_the_project_keeps_its_newest_runs_whichever_experiment_they_are_of(
    store: SQLStoreBase[Any], project: _Project
) -> None:
    older, newer = project.log(0, 3), project.log(1, 3)
    assert _find(store, project, RunCount(max_runs=3, unit=RetentionUnit.PROJECT)) == older
    assert newer


def test_a_kept_run_is_neither_counted_nor_deleted(
    store: SQLStoreBase[Any], project: _Project, admin: models.User
) -> None:
    runs = project.log(0, 7)
    store.keep_runs(frozenset(runs[:2]), admin)
    assert _find(store, project, RunCount(max_runs=2)) == runs[2:5]
    store.keep_runs(frozenset(runs[2:3]), admin)
    assert _find(store, project, RunCount(max_runs=2)) == []


@pytest.mark.parametrize(("days_ago", "deleted"), [(0, False), (8, True)], ids=["running", "abandoned"])
def test_a_run_still_running_is_counted_but_not_deleted_until_its_grace_is_over(
    store: SQLStoreBase[Any], project: _Project, days_ago: int, *, deleted: bool
) -> None:
    (running,) = project.log(0, 1, days_ago=days_ago, status=RunStatus.RUNNING)
    rest = project.log(0, 5)
    assert _find(store, project, RunCount(max_runs=3)) == ([running] if deleted else []) + rest[:2]


@pytest.mark.parametrize(
    ("ages", "deleted"), [((3, 6, 7), [7, 6]), ((3, 5.5), [])], ids=["past the slack", "within it"]
)
def test_an_age_acts_past_its_slack_and_deletes_every_run_older_than_it(
    store: SQLStoreBase[Any], project: _Project, ages: tuple[float, ...], deleted: list[float]
) -> None:
    runs = {age: project.log(0, 1, days_ago=age)[0] for age in ages}
    assert _find(store, project, RunAge(max_age_days=5)) == [runs[age] for age in deleted]


def test_an_experiment_held_to_a_stricter_rule_deletes_down_to_its_own(
    store: SQLStoreBase[Any], project: _Project
) -> None:
    strict, lax = project.log(0, 6), project.log(1, 6)
    assert _find(store, project, RunCount(max_runs=10), e0=RunCount(max_runs=3)) == strict[:3]
    assert lax


def test_an_experiments_rule_that_keeps_more_than_its_project_is_not_applied(
    store: SQLStoreBase[Any], project: _Project
) -> None:
    runs = project.log(0, 6)
    assert _find(store, project, RunCount(max_runs=3), e0=Unlimited()) == runs[:3]


def test_candidates_are_the_oldest_first_at_most_the_limit_and_never_of_a_deleted_experiment(
    store: SQLStoreBase[Any], project: _Project, admin: models.User
) -> None:
    first, second = project.log(0, 6), project.log(1, 6)
    found = Candidates(store, RetentionSettings(), pendulum.now("UTC"))
    rules = Rules(project=RunCount(max_runs=1), experiments={})
    assert found.find(project.id, rules, limit=4) == first[:4]
    store.delete_experiment(project.experiments[0], admin)
    assert found.find(project.id, rules, limit=100) == second[:5]


def test_a_count_of_what_is_overdue_agrees_with_what_is_found_and_stops_at_its_limit(
    store: SQLStoreBase[Any], project: _Project, admin: models.User
) -> None:
    older, newer = project.log(0, 9), project.log(1, 9, days_ago=1)
    project.log(0, 1, days_ago=8, status=RunStatus.RUNNING)  # abandoned: found
    project.log(0, 1, days_ago=2, status=RunStatus.RUNNING)  # still running: neither found nor counted
    store.keep_runs(frozenset(older[:2]), admin)
    rules = Rules(project=RunCount(max_runs=3), experiments={})
    candidates = Candidates(store, RetentionSettings(), pendulum.now("UTC"))
    found = candidates.find(project.id, rules, limit=1_000)
    assert len(found) > 10
    assert candidates.overdue(project.id, rules, limit=1_000) == len(found)
    assert candidates.overdue(project.id, rules, limit=4) == 4
    assert newer


def test_a_batch_reads_about_its_limit_however_many_experiments_are_behind(
    store: SQLStoreBase[Any], project: _Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.log(0, 30)
    project.log(1, 30)
    reads: list[int | None] = []
    get_runs = store.get_runs

    def spying(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if kwargs.get("created_before") is not None:  # what is read to be deleted, not to find where to cut
            reads.append(kwargs["limit"])
        return get_runs(*args, **kwargs)

    monkeypatch.setattr(store, "get_runs", spying)
    rules = Rules(project=RunCount(max_runs=1), experiments={})
    found = Candidates(store, RetentionSettings(), pendulum.now("UTC")).find(project.id, rules, limit=5)
    assert len(found) == 5
    assert len(reads) == 1  # the first experiment gave all 5: the second was not read at all
