"""`fetch_series_band`: across some keys, the spread of their values at each step of a run, on a grid of steps."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pendulum
import pytest

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend

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


def _band(store: SQLLiteStore, experiment_id: int, keys: set[str], grid: int) -> list[models.SeriesBand]:
    return store.fetch_series_band(experiment_id, keys=frozenset(keys), run_ids=None, grid=grid)


def test_the_band_of_five_keys_is_their_nearest_ranks(store: SQLLiteStore, experiment_id: int) -> None:
    (run,) = (store.create_run(models.NewRun(experiment_id=experiment_id)),)
    _log(store, run, 0, {f"k{value}": float(value) for value in range(1, 6)})

    assert _band(store, experiment_id, {f"k{value}" for value in range(1, 6)}, grid=8) == [
        models.SeriesBand(run.id, 0, 5, 1.0, 2.0, 3.0, 4.0, 5.0, 3.0)
    ]


def test_a_step_counts_only_the_keys_that_logged_a_number_there(
    store: SQLLiteStore, experiment_id: int
) -> None:
    (run,) = (store.create_run(models.NewRun(experiment_id=experiment_id)),)
    _log(store, run, 0, {"a": 1.0, "b": 3.0, "c": math.nan})
    _log(store, run, 1, {"d": 7.0})

    band = _band(store, experiment_id, {"a", "b", "c", "d"}, grid=8)

    assert band == [
        models.SeriesBand(run.id, 0, 2, 1.0, 1.0, 1.0, 3.0, 3.0, 2.0),
        models.SeriesBand(run.id, 1, 1, 7.0, 7.0, 7.0, 7.0, 7.0, 7.0),
    ]


def test_a_step_logged_twice_counts_its_newest_write(store: SQLLiteStore, experiment_id: int) -> None:
    (run,) = (store.create_run(models.NewRun(experiment_id=experiment_id)),)
    _log(store, run, 0, {"a": 10.0, "b": 5.0})
    _log(store, run, 0, {"a": 20.0})

    assert _band(store, experiment_id, {"a", "b"}, grid=8) == [
        models.SeriesBand(run.id, 0, 2, 5.0, 5.0, 5.0, 20.0, 20.0, 12.5)
    ]


def test_a_newest_write_that_is_not_a_number_leaves_that_key_with_no_value_there(
    store: SQLLiteStore, experiment_id: int
) -> None:
    (run,) = (store.create_run(models.NewRun(experiment_id=experiment_id)),)
    _log(store, run, 0, {"a": 1.0, "b": 4.0})
    _log(store, run, 0, {"a": math.nan})

    assert _band(store, experiment_id, {"a", "b"}, grid=8) == [
        models.SeriesBand(run.id, 0, 1, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0)
    ]


@pytest.mark.parametrize(
    ("last", "grid", "steps"),
    [
        pytest.param(9, 10, list(range(10)), id="every-step-fits"),
        pytest.param(9, 4, [0, 4, 8, 9], id="a-stride-of-two-then-four-with-the-last-step"),
        pytest.param(50, 32, list(range(0, 51, 2)), id="a-stride-of-two-keeps-the-grid-under-the-cap"),
    ],
)
def test_the_grid_is_as_fine_as_the_cap_allows_and_always_ends_on_the_last_step(
    store: SQLLiteStore, experiment_id: int, last: int, grid: int, steps: list[int]
) -> None:
    (run,) = (store.create_run(models.NewRun(experiment_id=experiment_id)),)
    for step in range(last + 1):
        _log(store, run, step, {"a": float(step)})

    assert [band.step for band in _band(store, experiment_id, {"a"}, grid=grid)] == steps


def test_each_run_is_banded_apart(store: SQLLiteStore, experiment_id: int) -> None:
    first, second = (store.create_run(models.NewRun(experiment_id=experiment_id)) for _ in range(2))
    _log(store, first, 0, {"a": 1.0})
    _log(store, second, 0, {"a": 9.0})

    assert _band(store, experiment_id, {"a"}, grid=8) == [
        models.SeriesBand(first.id, 0, 1, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
        models.SeriesBand(second.id, 0, 1, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0),
    ]
