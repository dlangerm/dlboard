"""`summarize_*`: what an experiment logged, answered without reading what was logged."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend
from dlboard.serve._backend._metric_frame import MetricKeySummary

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

_TS = pendulum.datetime(2026, 1, 1, tz="UTC")
_ALICE = models.Principal.unverified("alice")


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


def _log(store: SQLLiteStore, run: models.Run, step: int, **metrics: float) -> None:
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics=metrics, step=step, experiment_id=run.experiment_id, run_id=run.id, timestamp_utc=_TS
            )
        ]
    )


def _other_experiment(store: SQLLiteStore) -> int:
    project = store.create_project(models.NewProject(name="other", description="d"))
    return store.create_experiment(models.NewExperiment(project_id=project.id)).id


def test_summarize_metric_keys_says_which_keys_every_run_logged_once(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    _log(store, run, 0, loss=0.5, acc=0.1)
    _log(store, run, 1, loss=0.4)

    assert store.summarize_metric_keys(experiment_id) == ["acc", "loss"]
    assert store.summarize_metric_steps(experiment_id, frozenset({"acc", "loss"})) == [
        MetricKeySummary("acc", max_steps_per_run=1),
        MetricKeySummary("loss", max_steps_per_run=2),
    ]


@given(logged=st.lists(st.tuples(st.integers(0, 2), st.integers(0, 9)), max_size=8))
@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])  # an experiment each
def test_summarize_metric_steps_is_the_longest_span_a_run_logged_a_key_over(
    store: SQLLiteStore, experiment_id: int, logged: list[tuple[int, int]]
) -> None:
    """Exactly 1 when no run logged two steps (re-sending one is no second step), and 0 when none logged it."""
    shared = store.get_experiment(experiment_id)
    assert shared is not None
    experiment = store.create_experiment(models.NewExperiment(project_id=shared.project_id)).id
    runs = [store.create_run(models.NewRun(experiment_id=experiment)) for _ in range(3)]
    for run, step in logged:
        _log(store, runs[run], step, m=1.0)
    steps = [[step for at, step in logged if at == run] for run in range(3)]

    assert store.summarize_metric_steps(experiment, frozenset({"m"})) == [
        MetricKeySummary("m", max((max(run) - min(run) + 1 for run in steps if run), default=0))
    ]


def test_summarize_metric_steps_judges_a_key_on_the_newest_runs_only(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """The oldest run logged two steps, but it is older than the newest runs a summary looks at, so it does not count."""
    # Creation times are distinct and ascending: runs made in one tight loop can tie, and then "newest" is a coin toss.
    oldest = store.create_run(models.NewRun(experiment_id=experiment_id, created_at=_TS))
    _log(store, oldest, 0, m=1.0)
    _log(store, oldest, 1, m=1.0)
    for index in range(1, 101):
        newer = store.create_run(
            models.NewRun(experiment_id=experiment_id, created_at=_TS.add(seconds=index))
        )
        _log(store, newer, 0, m=1.0)

    assert store.summarize_metric_steps(experiment_id, frozenset({"m"})) == [
        MetricKeySummary("m", max_steps_per_run=1)
    ]


def test_summarize_metric_keys_excludes_deleted_runs_and_other_experiments(
    store: SQLLiteStore, experiment_id: int
) -> None:
    other = store.create_run(models.NewRun(experiment_id=_other_experiment(store)))
    deleted = store.create_run(models.NewRun(experiment_id=experiment_id))
    kept = store.create_run(models.NewRun(experiment_id=experiment_id))
    _log(store, other, 0, other_metric=1.0)
    _log(store, deleted, 0, deleted_run_metric=1.0, shared=1.0)
    _log(store, deleted, 1, shared=1.0)
    _log(store, kept, 0, shared=1.0)
    store.delete_run(deleted.id, store.get_or_create_user(_ALICE))

    # `shared` has two steps, but only in the deleted run: it is still a key every live run logged once.
    assert store.summarize_metric_keys(experiment_id) == ["shared"]
    assert store.summarize_metric_steps(experiment_id, frozenset({"shared"})) == [
        MetricKeySummary("shared", max_steps_per_run=1)
    ]


def _artifact(run: models.Run, key: str, step: int) -> models.Artifact:
    return models.Artifact(
        key=key,
        fname=f"{key}.png",
        run_id=run.id,
        experiment_id=run.experiment_id,
        step=step,
        ref=f"ref://{key}/{run.id}/{step}",
    )


def test_summarize_artifact_keys_returns_the_latest_live_artifact_of_each_key(
    store: SQLLiteStore, experiment_id: int
) -> None:
    first, second = (store.create_run(models.NewRun(experiment_id=experiment_id)) for _ in range(2))
    other = store.create_run(models.NewRun(experiment_id=_other_experiment(store)))
    store.log_artifact_refs(
        [
            *(_artifact(first, "img", step) for step in range(3)),
            _artifact(second, "img", 0),
            _artifact(first, "only_first", 4),
            *(_artifact(first, "gone", step) for step in range(2)),
            _artifact(other, "elsewhere", 0),
        ]
    )
    ids = {a.ref: a.id for a in store.fetch_artifacts(experiment_id, run_ids=None) if a.id is not None}
    actor = store.get_or_create_user(_ALICE)
    store.delete_artifact(ids[f"ref://img/{second.id}/0"], actor)
    for step in range(2):
        store.delete_artifact(ids[f"ref://gone/{first.id}/{step}"], actor)

    summary = store.summarize_artifact_keys(experiment_id)

    assert [(a.key, a.run_id, a.step) for a in summary] == [("img", first.id, 2), ("only_first", first.id, 4)]
