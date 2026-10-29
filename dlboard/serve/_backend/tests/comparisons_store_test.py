"""Comparisons: owned, project-scoped pages with a hand-picked set of runs, on every backend."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest
import sqlalchemy as sa

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


class _World(NamedTuple):
    project_id: int
    first: int
    """An experiment of `alpha` and `beta`."""
    second: int
    """An experiment of `gamma`."""
    run: dict[str, int]
    foreign: int
    """A run of another project, which no comparison here can hold."""
    alice: int
    bob: int
    admin: models.User


@pytest.fixture
def world(store: SQLLiteStore, admin: models.User) -> _World:
    project_id = store.create_project(models.NewProject(name="p", description="d")).id
    first, second = (
        store.create_experiment(models.NewExperiment(project_id=project_id)).id for _ in range(2)
    )
    runs = {
        name: store.create_run(models.NewRun(experiment_id=experiment, name=name)).id
        for name, experiment in (("alpha", first), ("beta", first), ("gamma", second))
    }
    elsewhere = store.create_project(models.NewProject(name="elsewhere", description="d"))
    foreign = store.create_run(
        models.NewRun(experiment_id=store.create_experiment(models.NewExperiment(project_id=elsewhere.id)).id)
    )
    alice, bob = (store.get_or_create_user(models.Principal.unverified(name)).id for name in ("alice", "bob"))
    return _World(project_id, first, second, runs, foreign.id, alice, bob, admin)


def _comparison(
    store: SQLLiteStore,
    w: _World,
    runs: set[int],
    *,
    owner: int | None = None,
    name: str = "mine",
) -> models.Page[Any, Any]:
    return store.create_comparison(
        models.Page[Any, Any],
        models.NewPage[Any, Any](project_id=w.project_id, owner_id=owner or w.alice, name=name),
        frozenset(runs),
    )


def _names(store: SQLLiteStore, page_id: int) -> list[str]:
    return [run.name or "" for run in store.comparison_runs(page_id)]


def _member_rows(store: SQLLiteStore) -> int:
    members = store._tables[models.ComparisonRun]
    return store._execute(sa.select(sa.func.count()).select_from(members))[0][0]


def test_a_comparison_holds_only_runs_of_its_project_that_are_still_there(
    store: SQLLiteStore, world: _World
) -> None:
    store.delete_run(world.run["beta"], world.admin)

    page = _comparison(
        store, world, {world.run["alpha"], world.run["beta"], world.run["gamma"], world.foreign}
    )

    assert page.project_id == world.project_id
    assert _names(store, page.id) == ["gamma", "alpha"]


def test_adding_says_how_many_runs_were_new_and_removing_ignores_what_is_not_a_member(
    store: SQLLiteStore, world: _World
) -> None:
    page = _comparison(store, world, {world.run["alpha"]})

    def add(*runs: int) -> int:
        return store.add_comparison_runs(page.id, frozenset(runs), added_by=world.bob)

    assert add(world.run["alpha"], world.run["beta"], world.foreign) == 1
    assert add(world.run["beta"]) == 0
    store.remove_comparison_runs(page.id, frozenset({world.run["alpha"], world.run["gamma"]}))

    assert _names(store, page.id) == ["beta"]


def test_a_comparison_past_its_cap_is_refused_whole_and_says_how_many_fit(
    store: SQLLiteStore, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLBOARD_MAX_COMPARISON_RUNS", "2")
    page = _comparison(store, world, {world.run["alpha"], world.run["beta"]})

    with pytest.raises(ValueError, match=r"at most 2 runs: it has 2, so 0 more fit"):
        store.add_comparison_runs(page.id, frozenset({world.run["gamma"]}), added_by=world.bob)
    with pytest.raises(ValueError, match=r"at most 2 runs: it has 0, so 2 more fit"):
        store.create_comparison(
            models.Page[Any, Any],
            models.NewPage[Any, Any](project_id=world.project_id, owner_id=world.alice, name="big"),
            frozenset(world.run.values()),
        )

    assert sorted(_names(store, page.id)) == ["alpha", "beta"]
    assert [c.name for c in store.list_comparisons(world.project_id, world.alice)] == ["mine"]
    store.add_comparison_runs(
        page.id, frozenset({world.run["alpha"]}), added_by=world.bob
    )  # already in it: fits


def test_restoring_a_deleted_member_cannot_take_a_comparison_past_its_cap(
    store: SQLLiteStore, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLBOARD_MAX_COMPARISON_RUNS", "2")
    page = _comparison(store, world, {world.run["alpha"], world.run["beta"]})
    store.delete_run(world.run["beta"], world.admin)  # still a member, and still counted

    with pytest.raises(ValueError, match="at most 2 runs"):
        store.add_comparison_runs(page.id, frozenset({world.run["gamma"]}), added_by=world.bob)

    store.restore_run(world.run["beta"], world.admin)
    assert sorted(_names(store, page.id)) == ["alpha", "beta"]


def test_a_moved_experiments_runs_leave_the_comparison_of_the_project_it_left(
    store: SQLLiteStore, world: _World
) -> None:
    page = _comparison(store, world, set(world.run.values()))
    elsewhere = store.create_project(models.NewProject(name="elsewhere 2", description="d")).id
    moved = store.get_experiment(world.second)
    assert moved is not None

    store.update_experiment(moved.model_copy(update={"project_id": elsewhere}))

    assert sorted(_names(store, page.id)) == ["alpha", "beta"]
    assert [c.run_count for c in store.list_comparisons(world.project_id, world.alice)] == [2]
    assert store.add_comparison_runs(page.id, frozenset({world.run["gamma"]}), added_by=world.bob) == 0


def test_a_comparison_names_its_owner_and_a_summary_finds_one(store: SQLLiteStore, world: _World) -> None:
    page = _comparison(store, world, {world.run["alpha"]}, owner=world.bob, name="named")

    summary = store.comparison_summary(page.id)

    assert summary is not None
    assert (summary.name, summary.owner_name, summary.run_count) == ("named", "bob", 1)
    assert store.list_comparisons(world.project_id, world.alice) == []  # private: not alice's to list
    assert store.comparison_summary(999) is None
    assert (
        store.comparison_summary(
            store.get_or_create_page(models.Page[Any, Any], experiment_id=world.first).id
        )
        is None
    )


def test_a_projects_page_is_always_someones_comparison_never_a_shared_page_of_the_project(
    store: SQLLiteStore, world: _World
) -> None:
    with pytest.raises(ValueError, match="are comparisons"):
        store.get_or_create_page(models.Page[Any, Any], project_id=world.project_id)


def test_a_user_lists_their_own_comparisons_first_then_shared_ones_and_never_a_private_one(
    store: SQLLiteStore, world: _World
) -> None:
    mine = _comparison(store, world, {world.run["alpha"], world.run["gamma"]}, owner=world.bob, name="zeta")
    _comparison(store, world, set(), owner=world.bob, name="alpha")
    theirs = _comparison(store, world, {world.run["beta"]}, owner=world.alice, name="shared")
    store.update_page(theirs.model_copy(update={"shared": True}))
    _comparison(store, world, set(), owner=world.alice, name="private")

    listed = store.list_comparisons(world.project_id, world.bob)

    assert [(c.name, c.owner_id, c.shared, c.run_count) for c in listed] == [
        ("alpha", world.bob, False, 0),
        ("zeta", world.bob, False, 2),
        ("shared", world.alice, True, 1),
    ]
    assert listed[1].id == mine.id
    assert listed[2].id == theirs.id
    assert store.list_comparisons(world.project_id + 100, world.bob) == []


@pytest.mark.parametrize("delete", ["run", "experiment"])
def test_a_deleted_run_or_experiment_leaves_a_comparison_until_it_is_restored(
    store: SQLLiteStore, world: _World, delete: str
) -> None:
    page = _comparison(store, world, set(world.run.values()))

    if delete == "run":
        store.delete_run(world.run["gamma"], world.admin)
    else:
        store.delete_experiment(world.second, world.admin)

    assert sorted(_names(store, page.id)) == ["alpha", "beta"]
    assert store.list_comparisons(world.project_id, world.alice)[0].run_count == 2

    if delete == "run":
        store.restore_run(world.run["gamma"], world.admin)
    else:
        store.restore_experiment(world.second, world.admin)

    assert sorted(_names(store, page.id)) == ["alpha", "beta", "gamma"]


def test_purging_a_run_or_deleting_a_comparison_removes_the_memberships_with_it(
    store: SQLLiteStore, world: _World
) -> None:
    kept = _comparison(store, world, set(world.run.values()), name="kept")
    gone = _comparison(store, world, set(world.run.values()), name="gone")
    assert _member_rows(store) == 6

    store.delete_run(world.run["alpha"], world.admin)
    store.purge_run(world.run["alpha"], world.admin)
    assert _member_rows(store) == 4

    store.delete_view(gone.id, world.alice)
    assert _member_rows(store) == 2
    assert sorted(_names(store, kept.id)) == ["beta", "gamma"]


def test_purging_a_project_counts_each_membership_once_in_the_audit_log(
    store: SQLLiteStore, world: _World
) -> None:
    _comparison(store, world, {world.run["alpha"], world.run["gamma"]})
    store.delete_project(world.project_id, world.admin)

    store.purge_project(world.project_id, world.admin)

    purge = next(e for e in store.list_audit_log(world.admin) if e.action == models.AuditAction.PURGE)
    assert json.loads(purge.details)["ComparisonRun"] == 2
    assert _member_rows(store) == 0


@pytest.mark.parametrize(
    ("scope", "view_ok"),
    [
        pytest.param({}, False, id="none"),
        pytest.param({"experiment_id": 1}, True, id="an-experiment"),
        pytest.param({"project_id": 1, "experiment_id": 1}, False, id="two"),
    ],
)
def test_a_page_is_scoped_to_exactly_one_thing_and_a_comparison_to_a_project(
    store: SQLLiteStore, world: _World, scope: dict[str, int], *, view_ok: bool
) -> None:
    page = models.NewPage[Any, Any](owner_id=world.alice, name="x", **scope)

    if view_ok:
        store.create_view(models.Page[Any, Any], page)
    else:
        with pytest.raises(ValueError, match=r"scope|one of"):
            store.create_view(models.Page[Any, Any], page)
    with pytest.raises(ValueError, match=r"scope|one of"):
        store.create_comparison(models.Page[Any, Any], page, frozenset())


def test_a_pages_scope_cannot_be_changed_by_updating_it(store: SQLLiteStore, world: _World) -> None:
    page = _comparison(store, world, set())

    with pytest.raises(ValueError, match="scope cannot change"):
        store.update_page(page.model_copy(update={"project_id": None, "experiment_id": world.first}))
    renamed = store.update_page(page.model_copy(update={"name": "renamed"}))

    assert (renamed.name, renamed.project_id) == ("renamed", world.project_id)


def test_adding_to_a_comparison_that_does_not_exist_is_refused(store: SQLLiteStore, world: _World) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        store.add_comparison_runs(999, frozenset({world.run["alpha"]}), added_by=world.alice)
