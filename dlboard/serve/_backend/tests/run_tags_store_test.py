"""Run tags: a project's labels on its runs, shared, normalized, found by tag, listed a page at a time (#700)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum
import pytest
import sqlalchemy as sa

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


def _runs(store: SQLLiteStore, experiment_id: int, count: int) -> list[int]:
    return [store.create_run(models.NewRun(experiment_id=experiment_id)).id for _ in range(count)]


def test_a_run_is_given_each_tag_once_whatever_its_case_or_spacing(
    store: SQLLiteStore, experiment_id: int, admin: models.User
) -> None:
    first, second, untagged = _runs(store, experiment_id, 3)

    store.tag_runs(frozenset({first, second}), frozenset({"Baseline ", "ablation/lr"}), admin)
    store.tag_runs(frozenset({first}), frozenset({"baseline"}), admin)

    assert store.run_tags(frozenset({first, second, untagged})) == {
        first: ["ablation/lr", "baseline"],
        second: ["ablation/lr", "baseline"],
    }


def test_a_list_finds_the_runs_with_any_of_the_tags_it_is_filtered_to(
    store: SQLLiteStore, experiment_id: int, admin: models.User
) -> None:
    baseline, ablation, other = _runs(store, experiment_id, 3)
    store.tag_runs(frozenset({baseline}), frozenset({"baseline"}), admin)
    store.tag_runs(frozenset({ablation}), frozenset({"ablation"}), admin)

    def tagged(*tags: str) -> set[int]:
        return {run.id for run in store.get_runs(experiment_id, run_ids=None, tags=frozenset(tags))}

    assert tagged("Baseline") == {baseline}
    assert tagged("baseline", "ablation") == {baseline, ablation}
    assert tagged() == {baseline, ablation, other}


def test_a_projects_tags_are_listed_by_name_a_page_at_a_time(
    store: SQLLiteStore, experiment_id: int, admin: models.User
) -> None:
    (run,) = _runs(store, experiment_id, 1)
    store.tag_runs(frozenset({run}), frozenset({"seed-1", "seed-2", "baseline"}), admin)
    project_id = store.get_experiment(experiment_id).project_id  # pyrefly: ignore [missing-attribute]

    assert store.search_tags(project_id, limit=2) == ["baseline", "seed-1"]
    assert store.search_tags(project_id, search="SEED", limit=10) == ["seed-1", "seed-2"]
    assert store.search_tags(project_id + 1000, limit=10) == []


def test_untagging_takes_the_tag_off_those_runs_alone(
    store: SQLLiteStore, experiment_id: int, admin: models.User
) -> None:
    first, second = _runs(store, experiment_id, 2)
    store.tag_runs(frozenset({first, second}), frozenset({"baseline"}), admin)

    store.untag_runs(frozenset({first}), frozenset({"Baseline"}))

    assert store.run_tags(frozenset({first, second})) == {second: ["baseline"]}


def test_a_deleted_run_cannot_be_tagged_nor_the_runs_tagged_with_it(
    store: SQLLiteStore, experiment_id: int, admin: models.User
) -> None:
    live, deleted = _runs(store, experiment_id, 2)
    store.delete_run(deleted, admin)

    with pytest.raises(models.EntityGoneError):
        store.tag_runs(frozenset({live, deleted}), frozenset({"baseline"}), admin)

    project_id = store.get_experiment(experiment_id).project_id  # pyrefly: ignore [missing-attribute]
    assert (store.run_tags(frozenset({live})), store.search_tags(project_id, limit=10)) == ({}, [])


def test_a_tag_on_a_run_says_who_put_it_there_and_when(
    store: SQLLiteStore, experiment_id: int, admin: models.User
) -> None:
    (run,) = _runs(store, experiment_id, 1)
    before = pendulum.now(pendulum.UTC)

    store.tag_runs(frozenset({run}), frozenset({"baseline"}), admin)

    table = store._tables[models.RunTag]
    (tagged,) = (models.RunTag.model_validate(row._mapping) for row in store._execute(sa.select(table)))
    assert tagged.tagged_by == admin.id
    assert tagged.tagged_at >= before
