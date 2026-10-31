"""Pins on runs: one a user and colour, listed per experiment and per project, and gone with their run, on every backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import pytest

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend
from dlboard.models import PinColor

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
    user: dict[str, int]
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
    users = {
        name: store.get_or_create_user(models.Principal.unverified(name)).id for name in ("alice", "bob")
    }
    return _World(project_id, first, second, runs, users, admin)


def _pin_everything(store: SQLLiteStore, w: _World) -> None:
    """Five pins, oldest first: alice's alpha and beta red, her gamma plain, then bob's alpha green and gamma red."""
    pin = store.pin_runs
    pin(w.user["alice"], frozenset({w.run["alpha"], w.run["beta"]}), PinColor.RED)
    pin(w.user["alice"], frozenset({w.run["gamma"]}))
    pin(w.user["bob"], frozenset({w.run["alpha"]}), PinColor.GREEN)
    pin(w.user["bob"], frozenset({w.run["gamma"]}), PinColor.RED)


class _Filter(NamedTuple):
    """`list_pinned_runs`' filters, by name: the pinners by username, the experiment as the world's `first` or `second`."""

    colors: frozenset[PinColor] | None = None
    pinned_by: frozenset[str] | None = None
    experiment: str | None = None
    name_contains: str = ""


def _listed(store: SQLLiteStore, w: _World, f: _Filter = _Filter()) -> set[str]:  # noqa: B008
    """The names of the runs `list_pinned_runs` lists under `f`, whatever the page."""
    found = store.list_pinned_runs(
        w.project_id,
        colors=f.colors,
        pinned_by=None if f.pinned_by is None else frozenset(w.user[name] for name in f.pinned_by),
        experiment_id=None if f.experiment is None else getattr(w, f.experiment),
        name_contains=f.name_contains,
        limit=100,
    )
    assert found.total == len(found.runs)
    return {run.run.name or "" for run in found.runs}


def test_a_run_has_one_pin_a_user_and_colour_and_unpinning_is_theirs_and_that_colours(
    store: SQLLiteStore, world: _World
) -> None:
    alpha, alice, bob = world.run["alpha"], world.user["alice"], world.user["bob"]
    store.pin_runs(alice, frozenset({alpha}))
    store.pin_runs(alice, frozenset({alpha}))
    store.pin_runs(alice, frozenset({alpha}), PinColor.RED)
    store.pin_runs(bob, frozenset({alpha}))

    pins = store.pins_of(world.first, run_ids=None)[alpha]
    assert sorted((p.pin.pinned_by, p.pin.color) for p in pins) == sorted(
        [(alice, PinColor.PLAIN), (alice, PinColor.RED), (bob, PinColor.PLAIN)]
    )

    store.unpin_runs(alice, frozenset({alpha}), PinColor.PLAIN)
    assert sorted(
        (p.pin.pinned_by, p.pin.color) for p in store.pins_of(world.first, run_ids=None)[alpha]
    ) == sorted([(alice, PinColor.RED), (bob, PinColor.PLAIN)])


def test_an_experiments_pins_are_only_those_of_the_runs_asked_for(store: SQLLiteStore, world: _World) -> None:
    _pin_everything(store, world)

    assert set(store.pins_of(world.first, run_ids=None)) == {world.run["alpha"], world.run["beta"]}
    assert set(store.pins_of(world.first, run_ids=frozenset({world.run["beta"]}))) == {world.run["beta"]}
    assert store.pins_of(world.first, run_ids=frozenset()) == {}
    assert set(store.pins_of(world.second, run_ids=None)) == {world.run["gamma"]}


_RED, _GREEN, _PLAIN = PinColor.RED, PinColor.GREEN, PinColor.PLAIN


@pytest.mark.parametrize(
    ("f", "expected"),
    [
        pytest.param(_Filter(), {"alpha", "beta", "gamma"}, id="every-run"),
        pytest.param(_Filter(colors=frozenset({_GREEN})), {"alpha"}, id="a-colour"),
        pytest.param(_Filter(colors=frozenset({_GREEN, _PLAIN})), {"alpha", "gamma"}, id="two-colours"),
        pytest.param(_Filter(pinned_by=frozenset({"bob"})), {"alpha", "gamma"}, id="a-pinner"),
        pytest.param(
            _Filter(pinned_by=frozenset({"alice", "bob"}), colors=frozenset({_GREEN})),
            {"alpha"},
            id="pinners-and-colour",
        ),
        pytest.param(_Filter(experiment="second"), {"gamma"}, id="experiment"),
        pytest.param(_Filter(name_contains="ALP"), {"alpha"}, id="name"),
    ],
)
def test_the_projects_list_keeps_the_runs_any_of_whose_pins_every_filter_matches(
    store: SQLLiteStore, world: _World, f: _Filter, expected: set[str]
) -> None:
    _pin_everything(store, world)

    assert _listed(store, world, f) == expected


def test_a_listed_run_is_one_row_with_every_pin_on_it_even_those_the_filter_did_not_match(
    store: SQLLiteStore, world: _World
) -> None:
    _pin_everything(store, world)

    (alpha,) = store.list_pinned_runs(world.project_id, colors=frozenset({_GREEN})).runs

    assert alpha.run.name == "alpha"
    assert alpha.experiment.id == world.first
    assert [(p.username, p.pin.color) for p in alpha.pins] == [("alice", _RED), ("bob", _GREEN)]


def test_the_projects_list_pages_newest_first_and_counts_every_match(
    store: SQLLiteStore, world: _World
) -> None:
    _pin_everything(store, world)

    newest = store.list_pinned_runs(world.project_id, limit=2)
    oldest = store.list_pinned_runs(world.project_id, limit=2, offset=2)

    assert (newest.total, oldest.total) == (3, 3)
    assert [r.run.name for r in newest.runs] == ["gamma", "alpha"]
    assert [r.run.name for r in oldest.runs] == ["beta"]
    assert newest.runs[0].experiment.id == world.second


@pytest.mark.parametrize("delete", ["run", "experiment"])
def test_a_deleted_run_or_experiment_hides_its_pins_until_it_is_restored(
    store: SQLLiteStore, world: _World, delete: str
) -> None:
    _pin_everything(store, world)
    gamma = world.run["gamma"]
    everything = _listed(store, world)

    if delete == "run":
        store.delete_run(gamma, world.admin)
    else:
        store.delete_experiment(world.second, world.admin)

    assert _listed(store, world) == everything - {"gamma"}
    assert store.pins_of(world.second, run_ids=None) == {}

    if delete == "run":
        store.restore_run(gamma, world.admin)
    else:
        store.restore_experiment(world.second, world.admin)

    assert _listed(store, world) == everything


def test_purging_a_run_removes_its_pins(store: SQLLiteStore, world: _World) -> None:
    _pin_everything(store, world)
    store.delete_run(world.run["alpha"], world.admin)

    store.purge_run(world.run["alpha"], world.admin)

    assert _listed(store, world) == {"beta", "gamma"}


def test_a_run_that_does_not_exist_or_was_deleted_cannot_be_pinned(
    store: SQLLiteStore, world: _World
) -> None:
    store.delete_run(world.run["beta"], world.admin)

    with pytest.raises(ValueError, match="has been deleted"):
        store.pin_runs(world.user["alice"], frozenset({world.run["alpha"], world.run["beta"]}))
    with pytest.raises(ValueError, match="does not exist"):
        store.pin_runs(world.user["alice"], frozenset({999_999}))
    assert store.list_pinned_runs(world.project_id).total == 0


def test_a_pin_names_who_pinned_it_and_a_project_lists_who_has_pinned(
    store: SQLLiteStore, world: _World
) -> None:
    _pin_everything(store, world)

    named = {p.username for runs in store.pins_of(world.first, run_ids=None).values() for p in runs}
    pinned = store.list_pinned_runs(world.project_id, limit=100)

    assert named == {"alice", "bob"}
    assert {p.username for r in pinned.runs for p in r.pins} == {"alice", "bob"}
    assert [
        (p.username, p.user_id == world.user[p.username]) for p in store.list_pinners(world.project_id)
    ] == [
        ("alice", True),
        ("bob", True),
    ]
    store.delete_experiment(world.second, world.admin)
    store.delete_run(world.run["alpha"], world.admin)
    assert [p.username for p in store.list_pinners(world.project_id)] == [
        "alice"
    ]  # bob pinned alpha and gamma only


def test_the_runs_pinned_are_each_one_run_the_last_pinned_first_whatever_the_colours(
    store: SQLLiteStore, world: _World
) -> None:
    _pin_everything(store, world)
    store.pin_runs(
        world.user["alice"], frozenset({world.run["gamma"]}), PinColor.BLUE
    )  # gamma: three pins, the newest last

    assert store.pinned_run_ids(world.project_id, limit=10) == [
        world.run["gamma"],
        world.run["alpha"],
        world.run["beta"],
    ]
    assert store.pinned_run_ids(world.project_id, limit=2) == [world.run["gamma"], world.run["alpha"]]
    assert store.pinned_run_ids(world.project_id, colors=frozenset({PinColor.GREEN}), limit=10) == [
        world.run["alpha"]
    ]
    assert store.pinned_run_ids(world.project_id, name_contains="BET", limit=10) == [world.run["beta"]]
    assert store.pinned_run_ids(world.project_id + 100, limit=10) == []


def test_a_pin_knows_its_runs_experiment_and_runs_experiments_are_told_whether_deleted(
    store: SQLLiteStore, world: _World
) -> None:
    store.delete_run(world.run["beta"], world.admin)

    assert store.run_experiments(frozenset({*world.run.values(), 999_999})) == {
        world.run["alpha"]: world.first,
        world.run["beta"]: world.first,
        world.run["gamma"]: world.second,
    }
    store.pin_runs(world.user["alice"], frozenset({world.run["alpha"], world.run["gamma"]}))
    assert {r.run.id: r.experiment.id for r in store.list_pinned_runs(world.project_id).runs} == {
        world.run["alpha"]: world.first,
        world.run["gamma"]: world.second,
    }


def test_more_runs_than_a_statement_binds_are_pinned_and_unpinned(store: SQLLiteStore, world: _World) -> None:
    runs = frozenset(store.create_run(models.NewRun(experiment_id=world.first)).id for _ in range(1200))
    alice = world.user["alice"]

    store.pin_runs(alice, runs, PinColor.RED)
    assert set(store.pins_of(world.first, run_ids=runs)) == runs
    store.unpin_runs(alice, runs, PinColor.RED)

    assert store.pins_of(world.first, run_ids=runs) == {}


def test_a_project_counts_its_runs_pinned_in_each_colour_once_whoever_pinned(
    store: SQLLiteStore, world: _World
) -> None:
    _pin_everything(store, world)
    store.pin_runs(world.user["bob"], frozenset({world.run["alpha"]}), PinColor.RED)  # alpha was red already

    assert store.pin_counts(world.project_id) == {_RED: 3, _GREEN: 1, _PLAIN: 1}

    store.delete_run(world.run["gamma"], world.admin)

    assert store.pin_counts(world.project_id) == {_RED: 2, _GREEN: 1}
    assert store.pin_counts(world.project_id + 100) == {}
