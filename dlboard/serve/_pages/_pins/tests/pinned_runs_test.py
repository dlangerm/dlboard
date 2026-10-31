"""The project page's pinned runs: the filter its address holds, and a row to a run however many pins it has."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dlboard import models
from dlboard.models import PinColor
from dlboard.serve._pages._pins import _pinned_runs as pinned_runs
from dlboard.serve._pages._pins._pinned_runs import PinFilter

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


@pytest.fixture(autouse=True)
def _plain_run_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """Links to a run are plain paths, with no running Dash app for `relative_path` to read the prefix of."""
    monkeypatch.setattr("dlboard.serve._pages._experiment._focus.relative_path", str)


@pytest.mark.parametrize(
    ("search", "expected"),
    [
        pytest.param("", PinFilter(), id="no-filter"),
        pytest.param(
            "?pins_color=red,blue&pins_by=3&pins_exp=7&pins_q=alp&pins_page=2",
            PinFilter(colors={PinColor.RED, PinColor.BLUE}, pinned_by={3}, experiment=7, q="alp", page=2),
            id="every-filter",
        ),
        pytest.param("?pins_color=mauve", PinFilter(), id="a-colour-that-does-not-exist-is-no-filter"),
        pytest.param("?pins_page=0", PinFilter(), id="a-page-that-does-not-exist-is-no-filter"),
        pytest.param("?tab=Images&pins_q=x", PinFilter(q="x"), id="other-parameters-are-left-alone"),
    ],
)
def test_the_address_says_which_runs_the_list_shows(search: str, expected: PinFilter) -> None:
    assert PinFilter.from_search(search) == expected


def test_a_run_pinned_in_many_colours_by_many_people_is_one_row_with_all_its_pins(
    store: SQLLiteStore, experiment_id: int
) -> None:
    project_id = store.get_experiment(experiment_id).project_id  # pyrefly: ignore [missing-attribute]
    alpha, beta = (
        store.create_run(models.NewRun(experiment_id=experiment_id, name=n)) for n in ("alpha", "beta")
    )
    alice, bob = (store.get_or_create_user(models.Principal.unverified(n)).id for n in ("alice", "bob"))
    for user in (alice, bob):
        for color in (PinColor.RED, PinColor.BLUE, PinColor.GREEN):
            store.pin_runs(user, frozenset({alpha.id}), color)
    store.pin_runs(alice, frozenset({beta.id}), PinColor.PLAIN)

    found, pages = pinned_runs._fetch(store, project_id, PinFilter())
    rows = pinned_runs._rows(found, alice)

    assert pages == 1
    assert sorted(row["run_name"] for row in rows) == ["alpha", "beta"]
    (alpha_row,) = (row for row in rows if row["run_name"] == "alpha")
    assert [(pin["color"], pin["who"]) for pin in alpha_row["_pins"]] == [
        ("red", ["alice", "bob"]),
        ("green", ["alice", "bob"]),
        ("blue", ["alice", "bob"]),
    ]
    assert alpha_row["_href"] == f"/experiment/{experiment_id}?run={alpha.id}"


def test_a_page_past_the_end_shows_the_last_one(store: SQLLiteStore, experiment_id: int) -> None:
    project_id = store.get_experiment(experiment_id).project_id  # pyrefly: ignore [missing-attribute]
    user = store.get_or_create_user(models.Principal.unverified("alice")).id
    runs = frozenset(
        store.create_run(models.NewRun(experiment_id=experiment_id)).id
        for _ in range(pinned_runs.PAGE_SIZE + 3)
    )
    store.pin_runs(user, runs, PinColor.RED)

    found, pages = pinned_runs._fetch(store, project_id, PinFilter(page=99))

    assert (pages, found.total, len(found.runs)) == (2, pinned_runs.PAGE_SIZE + 3, 3)
