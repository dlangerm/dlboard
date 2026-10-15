"""Tests for logging, thinning and reading histograms, on every backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum
import pytest
import sqlalchemy as sa

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

_TS = pendulum.datetime(2026, 1, 1, tz="UTC")
_KEY = frozenset({"grad"})


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


def _log(
    store: SQLLiteStore, run: models.Run, steps: Iterable[int], key: str = "grad", count: int = 1
) -> None:
    """Log one histogram per step, whose single bucket's count says which write it was."""
    store.log_metrics(
        [
            models.LoggedMetrics(
                histograms={key: models.Histogram(edges=[0.0, 1.0], counts=[count])},
                step=step,
                experiment_id=run.experiment_id,
                run_id=run.id,
                timestamp_utc=_TS,
            )
            for step in steps
        ]
    )


def _steps(store: SQLLiteStore, experiment_id: int, max_steps: int) -> list[int]:
    return [h.step for h in store.fetch_histograms(experiment_id, keys=_KEY, max_steps=max_steps)]


def test_a_histogram_reads_back_as_it_was_logged(store: SQLLiteStore, experiment_id: int) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    histogram = models.Histogram(edges=[-1.5, 0.0, 1e-12, 3.25], counts=[4, 0, 9], nonfinite=2)
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"loss": 1.0},
                histograms={"grad": histogram},
                step=3,
                experiment_id=experiment_id,
                run_id=run.id,
                timestamp_utc=_TS,
            )
        ]
    )

    (entry,) = store.fetch_histograms(experiment_id, keys=_KEY, max_steps=10)

    assert (entry.run_id, entry.key, entry.step, entry.timestamp_utc) == (run.id, "grad", 3, _TS)
    assert entry.histogram == histogram
    assert store.fetch_metrics(experiment_id).keys == {"loss"}


@pytest.mark.parametrize(
    ("total", "kept"),
    [
        (10, list(range(10))),
        (11, [0, 2, 4, 6, 8, 10]),
        (100, [0, 16, 32, 48, 64, 80, 96, 99]),
    ],
)
def test_a_long_run_is_thinned_by_a_power_of_two_keeping_its_first_and_latest_steps(
    store: SQLLiteStore, experiment_id: int, total: int, kept: list[int]
) -> None:
    _log(store, store.create_run(models.NewRun(experiment_id=experiment_id)), range(total))

    assert _steps(store, experiment_id, max_steps=10) == kept


def test_the_steps_kept_stay_put_while_a_run_trains_within_one_stride(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    _log(store, run, range(100))
    before = _steps(store, experiment_id, max_steps=10)

    _log(store, run, range(100, 131))
    after = _steps(store, experiment_id, max_steps=10)

    assert after == [0, 16, 32, 48, 64, 80, 96, 112, 128, 130]
    assert before[:-1] == after[: len(before) - 1]


def test_each_run_is_thinned_by_its_own_length(store: SQLLiteStore, experiment_id: int) -> None:
    long_run, short_run = (store.create_run(models.NewRun(experiment_id=experiment_id)) for _ in range(2))
    _log(store, long_run, range(100))
    _log(store, short_run, range(5))

    histograms = store.fetch_histograms(experiment_id, keys=_KEY, max_steps=10)

    assert [h.step for h in histograms if h.run_id == short_run.id] == list(range(5))
    assert len([h for h in histograms if h.run_id == long_run.id]) == 8


def test_a_step_logged_twice_reads_back_as_the_latest_write(store: SQLLiteStore, experiment_id: int) -> None:
    """A batch the client re-sends after a failure holds its steps again; thinning must not count them twice."""
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    _log(store, run, range(10), count=1)
    _log(store, run, range(10), count=2)

    histograms = store.fetch_histograms(experiment_id, keys=_KEY, max_steps=10)

    assert [h.step for h in histograms] == list(range(10))
    assert {h.counts[0] for h in histograms} == {2}


def test_reads_leave_out_other_keys_other_experiments_excluded_and_deleted_runs(
    store: SQLLiteStore, experiment_id: int
) -> None:
    other_project = store.create_project(models.NewProject(name="other", description="d"))
    other_experiment = store.create_experiment(models.NewExperiment(project_id=other_project.id))
    kept, excluded, deleted = (store.create_run(models.NewRun(experiment_id=experiment_id)) for _ in range(3))
    _log(store, kept, [0])
    _log(store, kept, [0], key="weights")
    _log(store, excluded, [0])
    _log(store, deleted, [0])
    _log(store, store.create_run(models.NewRun(experiment_id=other_experiment.id)), [0])
    store.delete_run(deleted.id, store.get_or_create_user(models.Principal.unverified("alice")))

    histograms = store.fetch_histograms(
        experiment_id, keys=_KEY, max_steps=10, exclude_run_ids=frozenset({excluded.id})
    )

    assert [(h.run_id, h.key) for h in histograms] == [(kept.id, "grad")]
    assert store.summarize_histogram_keys(experiment_id) == ["grad", "weights"]


def test_logging_a_histogram_bumps_the_experiment_revision_and_purging_the_run_removes_it(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    before = store.get_experiment(experiment_id)
    assert before is not None

    _log(store, run, [0])

    after = store.get_experiment(experiment_id)
    assert after is not None
    assert after.revision == before.revision + 1
    admin = store.get_or_create_user(models.Principal.unverified("admin"))
    store.delete_run(run.id, admin)
    store.purge_run(run.id, admin)
    table = store._tables[models.UnderlyingHistogramTableEntry]
    assert store._execute(sa.select(sa.func.count()).select_from(table)) == [(0,)]
