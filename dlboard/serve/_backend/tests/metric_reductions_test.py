"""`fetch_metrics` by reduction, and `fetch_latest_metrics`: a row per run and key, found without reading the steps."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

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

    assert store.fetch_latest_metrics(experiment_id) == {
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

    assert store.fetch_latest_metrics(experiment_id) == {run.id: {"loss": 0.5}}


def test_a_key_no_run_has_a_value_for_is_absent(store: SQLLiteStore, experiment_id: int) -> None:
    (run,) = _runs(store, experiment_id, 1)
    _log(store, run, 0, diverged=math.nan, loss=1.0)

    assert store.fetch_latest_metrics(experiment_id) == {run.id: {"loss": 1.0}}


def test_it_leaves_out_other_keys_other_experiments_deleted_and_excluded_runs(
    store: SQLLiteStore, experiment_id: int
) -> None:
    kept, excluded, deleted = _runs(store, experiment_id, 3)
    project = store.create_project(models.NewProject(name="other", description="d"))
    (elsewhere,) = _runs(store, store.create_experiment(models.NewExperiment(project_id=project.id)).id, 1)
    for run in (kept, excluded, deleted, elsewhere):
        _log(store, run, 0, loss=1.0, acc=0.5)
    store.delete_run(deleted.id, store.get_or_create_user(models.Principal.unverified("alice")))

    assert store.fetch_latest_metrics(
        experiment_id, keys=frozenset({"loss"}), exclude_run_ids=frozenset({excluded.id})
    ) == {kept.id: {"loss": 1.0}}


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

    frame = store.fetch_metrics(experiment_id, keys=frozenset({"loss"}), reduction=reduction)

    assert (list(frame.run_id), list(frame.step), list(frame.values("loss"))) == ([run.id], [step], [value])


def _log_series(
    store: SQLLiteStore, run: models.Run, values: Sequence[float | None], key: str = "loss"
) -> None:
    store.log_metrics(
        models.LoggedMetrics(
            metrics={key: value},
            step=step,
            experiment_id=run.experiment_id,
            run_id=run.id,
            timestamp_utc=_TS,
        )
        for step, value in enumerate(values)
    )


def _wave(steps: int) -> list[float]:
    return [math.sin(step / 40) + step % 7 / 50 for step in range(steps)]


def _thinned(store: SQLLiteStore, experiment_id: int, max_points: int) -> pd.DataFrame:
    return store.fetch_metrics(experiment_id, max_points=max_points).to_frame()


def test_a_series_within_the_budget_is_whole(store: SQLLiteStore, experiment_id: int) -> None:
    (run,) = _runs(store, experiment_id, 1)
    _log_series(store, run, _wave(80))

    assert list(_thinned(store, experiment_id, 100).step) == list(range(80))


def test_a_long_series_is_thinned_to_its_stretches_first_last_lowest_and_highest(
    store: SQLLiteStore, experiment_id: int
) -> None:
    (run,) = _runs(store, experiment_id, 1)
    values = _wave(2000)
    _log_series(store, run, values)

    thinned = _thinned(store, experiment_id, 50)

    assert len(thinned) <= 4 * 50
    assert {0, 1999} <= set(thinned.step)
    assert (thinned.loss.min(), thinned.loss.max()) == (min(values), max(values))
    assert [values[int(step)] for step in thinned.step] == list(thinned.loss)


def test_a_spike_survives_thinning(store: SQLLiteStore, experiment_id: int) -> None:
    (run,) = _runs(store, experiment_id, 1)
    values: list[float | None] = [0.0] * 5000
    values[1777] = 100.0
    _log_series(store, run, values)

    thinned = _thinned(store, experiment_id, 20)

    assert 1777 in set(thinned.step)
    assert len(thinned) <= 4 * 20


def test_a_series_that_never_changes_is_still_bounded(store: SQLLiteStore, experiment_id: int) -> None:
    (run,) = _runs(store, experiment_id, 1)
    _log_series(store, run, [0.5] * 5000)

    assert len(_thinned(store, experiment_id, 20)) <= 4 * 20


def test_the_points_kept_stay_put_as_a_run_trains(store: SQLLiteStore, experiment_id: int) -> None:
    """Until the stretches double, only the newest stretch changes: a poll does not pick others for the rest."""
    (run,) = _runs(store, experiment_id, 1)
    values = _wave(2000)
    _log_series(store, run, values)
    before = set(_thinned(store, experiment_id, 50).step)

    store.log_metrics(
        models.LoggedMetrics(
            metrics={"loss": 3.0}, step=step, experiment_id=experiment_id, run_id=run.id, timestamp_utc=_TS
        )
        for step in range(2000, 2010)
    )
    after = set(_thinned(store, experiment_id, 50).step)

    # 2000 steps in at most 50 stretches is 64 a stretch; the last one starts at 1984.
    assert {step for step in before if step < 1984} == {step for step in after if step < 1984}


def test_thinning_leaves_out_what_a_fetch_leaves_out(store: SQLLiteStore, experiment_id: int) -> None:
    kept, excluded, deleted = _runs(store, experiment_id, 3)
    project = store.create_project(models.NewProject(name="other", description="d"))
    (elsewhere,) = _runs(store, store.create_experiment(models.NewExperiment(project_id=project.id)).id, 1)
    for run in (kept, excluded, deleted, elsewhere):
        _log_series(store, run, [None if step % 100 == 0 else 1.0 + step for step in range(300)])
        _log_series(store, run, [0.0] * 300, key="other")
    store.delete_run(deleted.id, store.get_or_create_user(models.Principal.unverified("alice")))

    thinned = store.fetch_metrics(
        experiment_id, keys=frozenset({"loss"}), exclude_run_ids=frozenset({excluded.id}), max_points=50
    ).to_frame()

    assert set(thinned.run_id) == {kept.id}
    assert set(thinned.columns) == {"run_id", "step", "timestamp_utc", "loss"}
    assert 0 not in set(thinned.step)  # logged as `None`: no value, so no point
