"""A run's recorded context: kept once, as its hyperparameters are, and read only for the runs asked for."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dlboard import models

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


def _context(run: models.Run, commit: str) -> models.NewRunContext:
    return models.NewRunContext(
        run_id=run.id,
        experiment_id=run.experiment_id,
        git_commit=commit,
        command=["train.py", "--lr", "0.1"],
        packages={"torch": "2.4.0"},
    )


def test_a_run_keeps_the_first_context_recorded_for_it(store: SQLLiteStore, experiment_id: int) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    first = store.record_run_context(_context(run, "abc"))

    again = store.record_run_context(_context(run, "def"))

    assert again == first
    assert again.git_commit == "abc"
    assert (again.command, again.packages) == (["train.py", "--lr", "0.1"], {"torch": "2.4.0"})


def test_only_the_asked_for_live_runs_of_the_experiment_are_read(
    store: SQLLiteStore, experiment_id: int, admin: models.User
) -> None:
    asked, other, deleted = (store.create_run(models.NewRun(experiment_id=experiment_id)) for _ in range(3))
    for run in (asked, other, deleted):
        store.record_run_context(_context(run, f"commit-{run.id}"))
    store.delete_run(deleted.id, admin)
    another_experiment = store.create_experiment(models.NewExperiment(project_id=1)).id

    read = store.fetch_run_contexts(experiment_id, run_ids=frozenset({asked.id, deleted.id}))

    assert [context.run_id for context in read] == [asked.id]
    assert store.fetch_run_contexts(another_experiment, run_ids=frozenset({asked.id})) == []


def test_a_context_for_a_deleted_run_is_refused(
    store: SQLLiteStore, experiment_id: int, admin: models.User
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.delete_run(run.id, admin)

    with pytest.raises(models.EntityGoneError, match="has been deleted"):
        store.record_run_context(_context(run, "abc"))
