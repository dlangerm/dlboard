"""`fetch_metrics` by reduction: a row per run and key, found without reading the steps."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, cast

import pendulum
import pytest

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pandas as pd

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

_TS = pendulum.datetime(2026, 1, 1, tz="UTC")


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


def _log(store: SQLLiteStore, run: models.Run, step: int, **metrics: float | None) -> None:
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics=metrics, step=step, experiment_id=run.experiment_id, run_id=run.id, timestamp_utc=_TS
            )
        ]
    )


def _log_series(
    store: SQLLiteStore, run: models.Run, values: Sequence[float | None], key: str = "loss"
) -> None:
    for step, value in enumerate(values):
        _log(store, run, step, **{key: value})


def _runs(store: SQLLiteStore, experiment_id: int, count: int) -> list[models.Run]:
    return [store.create_run(models.NewRun(experiment_id=experiment_id)) for _ in range(count)]


def test_the_latest_value_is_the_one_at_the_highest_step_however_it_was_logged(
    store: SQLLiteStore, experiment_id: int
) -> None:
    ahead, behind = _runs(store, experiment_id, 2)
    _log(store, ahead, 2, loss=0.5)
    _log(store, ahead, 0, loss=9.0, acc=0.1)  # logged later, but at an earlier step
    _log(store, ahead, 4, loss=0.25)
    _log(store, ahead, 4, loss=0.75)  # a re-sent batch: the newest write wins
    _log(store, behind, 1, loss=0.5, epoch=3.0)

    assert store.fetch_metrics(
        experiment_id, run_ids=None, reduction=models.MetricReduction.LATEST
    ).latest_per_run() == {
        ahead.id: {"loss": 0.75, "acc": 0.1},
        behind.id: {"loss": 0.5, "epoch": 3.0},
    }


@pytest.mark.parametrize("missing", [None, math.nan], ids=["null", "nan"])
def test_a_missing_value_is_no_value_so_the_last_real_one_stands(
    store: SQLLiteStore, experiment_id: int, missing: float | None
) -> None:
    (run,) = _runs(store, experiment_id, 1)
    _log(store, run, 0, loss=0.5)
    _log(store, run, 1, loss=missing)

    assert store.fetch_metrics(
        experiment_id, run_ids=None, reduction=models.MetricReduction.LATEST
    ).latest_per_run() == {run.id: {"loss": 0.5}}


def test_a_key_no_run_has_a_value_for_is_absent(store: SQLLiteStore, experiment_id: int) -> None:
    (run,) = _runs(store, experiment_id, 1)
    _log(store, run, 0, diverged=math.nan, loss=1.0)

    assert store.fetch_metrics(
        experiment_id, run_ids=None, reduction=models.MetricReduction.LATEST
    ).latest_per_run() == {run.id: {"loss": 1.0}}


def test_it_leaves_out_other_keys_other_experiments_deleted_and_unasked_runs(
    store: SQLLiteStore, experiment_id: int
) -> None:
    kept, excluded, deleted = _runs(store, experiment_id, 3)
    project = store.create_project(models.NewProject(name="other", description="d"))
    (elsewhere,) = _runs(store, store.create_experiment(models.NewExperiment(project_id=project.id)).id, 1)
    for run in (kept, excluded, deleted, elsewhere):
        _log(store, run, 0, loss=1.0, acc=0.5)
    store.delete_run(deleted.id, store.get_or_create_user(models.Principal.unverified("alice")))

    assert store.fetch_metrics(
        experiment_id,
        keys=frozenset({"loss"}),
        run_ids=frozenset({kept.id, deleted.id}),
        reduction=models.MetricReduction.LATEST,
    ).latest_per_run() == {kept.id: {"loss": 1.0}}


@pytest.mark.parametrize(
    ("reduction", "value", "step"),
    [
        (models.MetricReduction.LATEST, 2.0, 4),
        (models.MetricReduction.FIRST, 5.0, 0),
        (models.MetricReduction.MIN, 1.0, 4),
        (models.MetricReduction.MAX, 5.0, 4),
        (models.MetricReduction.MEAN, 3.0, 4),
    ],
)
def test_a_reduction_is_a_row_per_run_and_key_of_the_steps_newest_writes(
    store: SQLLiteStore, experiment_id: int, reduction: models.MetricReduction, value: float, step: int
) -> None:
    run, empty_handed = _runs(store, experiment_id, 2)
    for at, loss in enumerate([5.0, 3.0, 9.0, 1.0, 2.0]):
        _log(store, run, at, loss=loss)
    _log(store, run, 2, loss=4.0)  # step 2 re-sent: only this one counts, so the 9 is no maximum
    _log(store, empty_handed, 0, other=1.0)

    frame = store.fetch_metrics(experiment_id, keys=frozenset({"loss"}), reduction=reduction, run_ids=None)

    assert (list(frame.run_id), list(frame.step), list(frame.values("loss"))) == ([run.id], [step], [value])


def _reduced(series: pd.Series[float], reduction: models.MetricReduction) -> float:
    match reduction:
        case models.MetricReduction.LATEST:
            return float(series.iloc[-1])
        case models.MetricReduction.FIRST:
            return float(series.iloc[0])
        case models.MetricReduction.MIN:
            return float(series.min())
        case models.MetricReduction.MAX:
            return float(series.max())
        case models.MetricReduction.MEAN:
            return float(series.mean())
        case models.MetricReduction.HISTORY:
            raise NotImplementedError


def _from_the_history(
    store: SQLLiteStore, experiment_id: int, reduction: models.MetricReduction
) -> dict[tuple[int, str], float]:
    """What `reduction` of each run's series is, worked out from every step in pandas: what it must agree with."""
    wide = store.fetch_metrics(experiment_id, run_ids=None).to_frame().sort_values(["run_id", "step"])
    return {
        (cast("int", run_id), key): _reduced(series, reduction)
        for run_id, run in wide.groupby("run_id")
        for key in (column for column in wide.columns if column not in models.MetricColumn)
        if len(series := run[key].dropna()) > 0
    }


@pytest.mark.parametrize(
    "reduction",
    [
        models.MetricReduction.LATEST,
        models.MetricReduction.FIRST,
        models.MetricReduction.MIN,
        models.MetricReduction.MAX,
        models.MetricReduction.MEAN,
    ],
)
def test_a_reduction_is_what_the_whole_history_reduces_to_however_steps_were_re_sent(
    store: SQLLiteStore, experiment_id: int, reduction: models.MetricReduction
) -> None:
    first, second = _runs(store, experiment_id, 2)
    _log_series(store, first, [5.0, 3.0, 4.0, 1.0, 2.0, 8.0, 6.0, 7.0, 9.0, 0.5], key="loss")
    _log_series(store, second, [2.0, None, 4.0, math.nan, 6.0], key="loss")
    _log(store, first, 9, loss=None)  # the newest write of the last step has no value: that step has none
    _log(store, first, 0, loss=math.nan)  # nor the first
    _log(store, first, 5, loss=99.0)  # the newest write of a step wins, whatever it was
    _log(store, second, 4, loss=-3.0)
    _log(store, second, 4, other=1.0)  # a different key at the same step is no re-send of this one

    reduced = store.fetch_metrics(experiment_id, reduction=reduction, run_ids=None).to_frame()

    got = {
        (cast("int", run_id), key): float(series.iloc[-1])
        for run_id, run in reduced.groupby("run_id")
        for key in ("loss", "other")
        if len(series := run[key].dropna()) > 0
    }
    expected = _from_the_history(store, experiment_id, reduction)
    assert got.keys() == expected.keys()
    assert got == pytest.approx(expected)


@pytest.mark.parametrize(
    ("reduction", "step"),
    [(models.MetricReduction.LATEST, 1), (models.MetricReduction.FIRST, 0)],
    ids=["latest", "first"],
)
def test_a_list_of_more_keys_than_sqlite_compounds_takes_in_one_query_reads_every_one(
    store: SQLLiteStore, experiment_id: int, reduction: models.MetricReduction, step: int
) -> None:
    """SQLite caps a UNION ALL at 500 SELECTs, so a list of keys longer than that must still be read in full."""
    (run,) = _runs(store, experiment_id, 1)
    keys = [f"metric_{i:04d}" for i in range(600)]
    for at in (0, 1):
        _log(store, run, at, **{key: float(i + 10 * at) for i, key in enumerate(keys)})

    assert store.fetch_metrics(
        experiment_id, keys=frozenset(keys), run_ids=None, reduction=reduction
    ).latest_per_run() == {run.id: {key: float(i + 10 * step) for i, key in enumerate(keys)}}
