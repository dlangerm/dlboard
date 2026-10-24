"""`fetch_series_band`: across groups of keys, the spread of their values over the cells of a run, on a grid of steps."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pendulum
import pytest

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend
from dlboard.serve._backend._sql_store_base import _grid_cells

if TYPE_CHECKING:
    from collections.abc import Mapping

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

_TS = pendulum.datetime(2026, 1, 1, tz="UTC")


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


def _log(store: SQLLiteStore, run: models.Run, step: int, metrics: Mapping[str, float]) -> None:
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics=dict(metrics),
                step=step,
                experiment_id=run.experiment_id,
                run_id=run.id,
                timestamp_utc=_TS,
            )
        ]
    )


def _band(
    store: SQLLiteStore, experiment_id: int, keys: set[str], grid: int, *, group: str = ""
) -> list[models.SeriesBand]:
    return store.fetch_series_band(experiment_id, groups=dict.fromkeys(keys, group), run_ids=None, grid=grid)


def _run(store: SQLLiteStore, experiment_id: int) -> models.Run:
    return store.create_run(models.NewRun(experiment_id=experiment_id))


def test_the_band_of_five_keys_is_their_nearest_ranks_and_says_which_keys_the_extremes_are(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = _run(store, experiment_id)
    _log(store, run, 0, {f"k{value}": float(value) for value in range(1, 6)})

    assert _band(store, experiment_id, {f"k{value}" for value in range(1, 6)}, grid=8) == [
        models.SeriesBand(run.id, "", 0, 5, 1.0, 2.0, 3.0, 4.0, 5.0, 3.0, "k1", "k5")
    ]


def test_a_cell_counts_only_the_keys_that_logged_a_number_in_it(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = _run(store, experiment_id)
    _log(store, run, 0, {"a": 1.0, "b": 3.0, "c": math.nan})
    _log(store, run, 1, {"d": 7.0})

    assert _band(store, experiment_id, {"a", "b", "c", "d"}, grid=8) == [
        models.SeriesBand(run.id, "", 0, 2, 1.0, 1.0, 1.0, 3.0, 3.0, 2.0, "a", "b"),
        models.SeriesBand(run.id, "", 1, 1, 7.0, 7.0, 7.0, 7.0, 7.0, 7.0, "d", "d"),
    ]


def test_a_step_logged_twice_counts_its_newest_write(store: SQLLiteStore, experiment_id: int) -> None:
    run = _run(store, experiment_id)
    _log(store, run, 0, {"a": 10.0, "b": 5.0})
    _log(store, run, 0, {"a": 20.0})

    assert _band(store, experiment_id, {"a", "b"}, grid=8) == [
        models.SeriesBand(run.id, "", 0, 2, 5.0, 5.0, 5.0, 20.0, 20.0, 12.5, "b", "a")
    ]


def test_a_newest_write_that_is_not_a_number_leaves_that_key_with_no_value_there(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = _run(store, experiment_id)
    _log(store, run, 0, {"a": 1.0, "b": 4.0})
    _log(store, run, 0, {"a": math.nan})

    assert _band(store, experiment_id, {"a", "b"}, grid=8) == [
        models.SeriesBand(run.id, "", 0, 1, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0, "b", "b")
    ]


@pytest.mark.parametrize(
    ("first", "last", "grid", "cells"),
    [
        pytest.param(0, 9, 10, [(i - 1, i) for i in range(10)], id="every-step-fits"),
        pytest.param(0, 9, 4, [(-1, 0), (0, 4), (4, 8), (8, 9)], id="a-stride-of-four-then-the-last-step"),
        pytest.param(
            0, 50, 32, [(i - 2, i) if i else (-1, 0) for i in range(0, 51, 2)], id="a-stride-of-two"
        ),
        pytest.param(
            1000, 1009, 4, [(999, 1000), (1000, 1004), (1004, 1008), (1008, 1009)], id="a-late-start"
        ),
    ],
)
def test_the_grid_runs_end_to_end_from_the_first_step_to_the_last_and_is_as_fine_as_the_cap_allows(
    first: int, last: int, grid: int, cells: list[tuple[int, int]]
) -> None:
    assert _grid_cells(first, last, grid) == cells


def test_a_run_that_logs_every_fifty_steps_has_a_band_wherever_it_logged(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """Lightning logs every `log_every_n_steps`; a grid that probed exact steps would find almost none of them."""
    run = _run(store, experiment_id)
    for step in range(49, 5000, 50):
        _log(store, run, step, {"a": float(step), "b": float(step + 1)})

    band = _band(store, experiment_id, {"a", "b"}, grid=32)

    assert len(band) == 21  # a cell of 256 steps every time, and the last step ending the 21st
    assert {b.count for b in band} == {2}
    assert band[-1].step == 4999
    assert band[-1].minimum == 4999.0


def test_a_cell_takes_each_keys_newest_step_in_it(store: SQLLiteStore, experiment_id: int) -> None:
    run = _run(store, experiment_id)
    for step in range(5):
        _log(store, run, step, {"a": float(step)})

    assert [(b.step, b.minimum) for b in _band(store, experiment_id, {"a"}, grid=2)] == [(0, 0.0), (4, 4.0)]


def test_the_grid_starts_where_the_keys_first_logged(store: SQLLiteStore, experiment_id: int) -> None:
    run = _run(store, experiment_id)
    for step in (1000, 1001, 1002):
        _log(store, run, step, {"a": float(step)})

    assert [b.step for b in _band(store, experiment_id, {"a"}, grid=8)] == [1000, 1001, 1002]


def test_each_run_is_banded_apart(store: SQLLiteStore, experiment_id: int) -> None:
    first, second = _run(store, experiment_id), _run(store, experiment_id)
    _log(store, first, 0, {"a": 1.0})
    _log(store, second, 0, {"a": 9.0})

    assert _band(store, experiment_id, {"a"}, grid=8) == [
        models.SeriesBand(first.id, "", 0, 1, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, "a", "a"),
        models.SeriesBand(second.id, "", 0, 1, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0, "a", "a"),
    ]


def test_each_group_is_banded_apart_in_the_one_read(store: SQLLiteStore, experiment_id: int) -> None:
    run = _run(store, experiment_id)
    _log(
        store,
        run,
        0,
        {"acc/1/dog": 1.0, "acc/2/dog": 3.0, "acc/1/cat": 10.0, "acc/2/cat": 30.0, "loss": 99.0},
    )

    bands = store.fetch_series_band(
        experiment_id,
        groups={"acc/1/dog": "dog", "acc/2/dog": "dog", "acc/1/cat": "cat", "acc/2/cat": "cat"},
        run_ids=None,
        grid=8,
    )

    assert bands == [
        models.SeriesBand(run.id, "cat", 0, 2, 10.0, 10.0, 10.0, 30.0, 30.0, 20.0, "acc/1/cat", "acc/2/cat"),
        models.SeriesBand(run.id, "dog", 0, 2, 1.0, 1.0, 1.0, 3.0, 3.0, 2.0, "acc/1/dog", "acc/2/dog"),
    ]


def test_a_key_with_quotes_and_percents_in_its_name_is_read_like_any_other(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = _run(store, experiment_id)
    awkward = 'it\'s 100%\\ "done" ?'
    _log(store, run, 0, {awkward: 2.0})

    assert [(b.minimum_key, b.maximum_key) for b in _band(store, experiment_id, {awkward}, grid=8)] == [
        (awkward, awkward)
    ]
