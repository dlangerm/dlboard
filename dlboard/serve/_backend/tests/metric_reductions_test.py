"""`fetch_metrics` by reduction: a row per run and key, found without reading the steps."""

from __future__ import annotations

import math
import sqlite3
from typing import TYPE_CHECKING

import pandas as pd
import pendulum
import pytest
import sqlalchemy as sa
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend
from dlboard.plugins.data_stores.sqlite import SQLLiteStore

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

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


def _runs(store: SQLLiteStore, experiment_id: int, count: int) -> list[models.Run]:
    return [store.create_run(models.NewRun(experiment_id=experiment_id)) for _ in range(count)]


_Write = tuple[int, int, str, float | None]
"""One logged value: the run (by index), its step, its key and its value; a step logged again is re-sent."""
_WRITES = st.lists(
    st.tuples(
        st.integers(0, 2),
        st.integers(0, 4),
        st.sampled_from(["loss", "acc"]),
        st.none() | st.just(math.nan) | st.floats(-10, 10, allow_nan=False),
    ),
    max_size=14,
)
_REDUCTIONS = st.sampled_from(
    [reduction for reduction in models.MetricReduction if reduction is not models.MetricReduction.HISTORY]
)


def _reduced(
    writes: list[_Write], reduction: models.MetricReduction
) -> dict[tuple[int, str], tuple[int, float]]:
    """
    Each run's and key's step and value under `reduction`, worked out from what was written, as it must come back.

    The newest write of a step is its value, and a missing one (`None` or NaN) is no value. The step is the latest
    with a value, but the first is the first's.
    """
    newest = {(run, key, step): value for run, step, key, value in writes}
    series: dict[tuple[int, str], list[tuple[int, float]]] = {}
    for (run, key, step), value in sorted(newest.items()):
        if value is not None and not math.isnan(value):
            series.setdefault((run, key), []).append((step, value))
    reduced: dict[tuple[int, str], tuple[int, float]] = {}
    for name, points in series.items():
        steps, values = [step for step, _ in points], [value for _, value in points]
        match reduction:
            case models.MetricReduction.LATEST:
                reduced[name] = (steps[-1], values[-1])
            case models.MetricReduction.FIRST:
                reduced[name] = (steps[0], values[0])
            case models.MetricReduction.MIN:
                reduced[name] = (steps[-1], min(values))
            case models.MetricReduction.MAX:
                reduced[name] = (steps[-1], max(values))
            case models.MetricReduction.MEAN:
                reduced[name] = (steps[-1], sum(values) / len(values))
            case models.MetricReduction.HISTORY:
                raise NotImplementedError
    return reduced


@given(writes=_WRITES, reduction=_REDUCTIONS)
@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])  # an experiment each
def test_a_reduction_is_a_row_per_run_and_key_of_what_the_steps_newest_writes_reduce_to(
    store: SQLLiteStore, experiment_id: int, writes: list[_Write], reduction: models.MetricReduction
) -> None:
    """A run or a key with no value is absent; a step logged later at an earlier step is still the earlier."""
    shared = store.get_experiment(experiment_id)
    assert shared is not None
    experiment = store.create_experiment(models.NewExperiment(project_id=shared.project_id)).id
    runs = _runs(store, experiment, 3)
    for run, step, key, value in writes:
        _log(store, runs[run], step, **{key: value})

    frame = store.fetch_metrics(experiment, reduction=reduction, run_ids=None).to_frame()

    got = {
        (next(at for at, run in enumerate(runs) if run.id == row["run_id"]), key): (row["step"], row[key])
        for row in frame.to_dict("records")
        for key in ("loss", "acc")
        if key in row and not pd.isna(row[key])
    }
    assert got == pytest.approx(_reduced(writes, reduction))


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
