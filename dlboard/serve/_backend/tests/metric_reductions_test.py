"""`fetch_metrics` by reduction: a row per run and key, found without reading the steps."""

from __future__ import annotations

import math
import sqlite3
from typing import TYPE_CHECKING, cast

import pendulum
import pytest
import sqlalchemy as sa

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend
from dlboard.plugins.data_stores.sqlite import SQLLiteStore

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import pandas as pd

    from dlboard.serve._backend._metric_frame import MetricRow

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


def _a_stock_sqlite(tmp_path: Path) -> tuple[SQLLiteStore, int, models.Run]:
    """
    A store whose SQLite has the limits a stock build does, not the larger ones of a distribution's.

    A statement binds at most 32,766 parameters and is at most 1,000,000 bytes long.
    """
    store = SQLLiteStore(tmp_path / "wide.sqlite")

    def a_stock_build(connection: sqlite3.Connection, _record: object) -> None:
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32_766)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 1_000_000)

    sa.event.listen(store._engine, "connect", a_stock_build)
    store._engine.dispose()  # a connection made before the listener would keep the build's own limits
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    (run,) = _runs(store, experiment.id, 1)
    return store, experiment.id, run


@pytest.mark.parametrize(
    "reduction",
    [
        models.MetricReduction.HISTORY,
        models.MetricReduction.LATEST,
        models.MetricReduction.FIRST,
        models.MetricReduction.MEAN,
    ],
)
def test_eleven_thousand_named_keys_are_read_within_the_limits_of_a_stock_sqlite(
    tmp_path: Path, reduction: models.MetricReduction
) -> None:
    store, experiment_id, run = _a_stock_sqlite(tmp_path)
    keys = [f"layer_{i:05d}/grad_norm/an_unusually_long_name" for i in range(11_000)]
    _log(store, run, 0, **dict.fromkeys(keys, 1.0))

    frame = store.fetch_metrics(experiment_id, keys=frozenset(keys), run_ids=None, reduction=reduction)

    assert frame.keys == frozenset(keys)
    assert len(store.summarize_metric_steps(experiment_id, frozenset(keys))) == len(keys)


@pytest.mark.parametrize("reduction", [models.MetricReduction.HISTORY, models.MetricReduction.MEAN])
def test_more_named_keys_than_a_statement_may_bind_are_read_in_full(
    tmp_path: Path, reduction: models.MetricReduction
) -> None:
    """The names of a list are written into the statement: past 32,766 of them they could not be bound."""
    store, experiment_id, run = _a_stock_sqlite(tmp_path)
    keys = [f"m{i:05d}" for i in range(33_000)]
    _log(store, run, 0, **dict.fromkeys(keys, 1.0))

    frame = store.fetch_metrics(experiment_id, keys=frozenset(keys), run_ids=None, reduction=reduction)

    assert frame.keys == frozenset(keys)


_AWKWARD = 'it\'s 100%: \\:x \\ "done" ? %s :named'


def test_a_key_with_quotes_percents_colons_and_backslashes_is_read_by_every_read_that_names_keys(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """The names of a read are written into its statement, so each character a statement means something by is tried."""
    first, second = _runs(store, experiment_id, 2)
    for run in (first, second):
        _log(store, run, 0, **{_AWKWARD: 1.0})
        _log(store, run, 1, **{_AWKWARD: 2.0})
    _log(store, second, 2, **{_AWKWARD: math.nan})
    keys = frozenset({_AWKWARD})

    def read(reduction: models.MetricReduction) -> dict[int, dict[str, float]]:
        return store.fetch_metrics(
            experiment_id, keys=keys, run_ids=None, reduction=reduction
        ).latest_per_run()

    assert read(models.MetricReduction.LATEST) == {first.id: {_AWKWARD: 2.0}, second.id: {_AWKWARD: 2.0}}
    assert read(models.MetricReduction.FIRST) == {first.id: {_AWKWARD: 1.0}, second.id: {_AWKWARD: 1.0}}
    assert [s.key for s in store.summarize_metric_steps(experiment_id, keys)] == [_AWKWARD]


def test_a_missing_last_value_is_looked_for_only_in_the_runs_and_keys_that_miss_it(
    store: SQLLiteStore, experiment_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One run's NaN in `x` and another's in `y` must not send `x` and `y` back through the series of both runs."""
    first, second = _runs(store, experiment_id, 2)
    for run in (first, second):
        _log(store, run, 0, x=1.0, y=2.0)
        _log(store, run, 1, x=3.0, y=4.0)
    _log(store, first, 2, x=math.nan, y=5.0)
    _log(store, second, 2, x=6.0, y=math.nan)
    asked: list[Sequence[tuple[int, str]]] = []
    settle = store._settled_ends

    def spy(experiment: int, pairs: Sequence[tuple[int, str]], *, latest: bool) -> list[MetricRow]:
        asked.append(pairs)
        return settle(experiment, pairs, latest=latest)

    monkeypatch.setattr(store, "_settled_ends", spy)

    frame = store.fetch_metrics(experiment_id, run_ids=None, reduction=models.MetricReduction.LATEST)

    assert frame.latest_per_run() == {first.id: {"x": 3.0, "y": 5.0}, second.id: {"x": 6.0, "y": 4.0}}
    assert asked == [[(first.id, "x"), (second.id, "y")]]
