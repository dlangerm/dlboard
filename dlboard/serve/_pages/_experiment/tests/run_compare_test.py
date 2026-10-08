"""Tests for the run-compare modal's URL state, row building and loading."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from dlboard import models
from dlboard.serve._pages._experiment._run_compare import (
    MAX_COMPARED_RUNS,
    CompareMode,
    CompareQuery,
    comparison_rows,
    load_comparison,
    run_field,
    visible_rows,
)

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


@pytest.mark.parametrize(
    ("query", "runs", "mode", "search"),
    [
        ({}, [], CompareMode.DIFF, ""),
        ({"compare": "3,7,9"}, [3, 7, 9], CompareMode.DIFF, ""),
        ({"compare": "3", "compare_mode": "all", "compare_q": "lr"}, [3], CompareMode.ALL, "lr"),
        ({"compare": "", "view": "4"}, [], CompareMode.DIFF, ""),
        # Malformed links open nothing rather than breaking the page.
        ({"compare": "3,x"}, [], CompareMode.DIFF, ""),
        ({"compare": ",".join(map(str, range(MAX_COMPARED_RUNS + 1)))}, [], CompareMode.DIFF, ""),
        ({"compare": "3", "compare_mode": "sideways"}, [], CompareMode.DIFF, ""),
    ],
)
def test_compare_query_parses_the_url(
    query: dict[str, str], runs: list[int], mode: CompareMode, search: str
) -> None:
    parsed = CompareQuery.from_query(query)

    assert (parsed.runs, parsed.mode, parsed.search) == (runs, mode, search)


def _rows() -> list[dict[str, Any]]:
    return comparison_rows(
        [1, 2],
        hparams={1: {"lr": 0.1, "opt": "adam", "seed": 0}, 2: {"lr": 0.1, "opt": "sgd"}},
        metrics={1: {"loss": 0.5}, 2: {"loss": 0.4}},
    )


def test_comparison_rows_list_hparams_then_metrics_and_flag_changes_from_the_baseline() -> None:
    rows = {row["key"]: row for row in _rows()}

    assert [row["key"] for row in _rows()] == ["lr", "opt", "seed", "loss"]
    assert rows["lr"]["_changed"] == []
    assert rows["opt"]["_changed"] == [run_field(2)]
    # A key only some runs logged counts as a difference.
    assert rows["seed"][run_field(2)] is None
    assert rows["seed"]["_differs"]
    assert rows["loss"]["kind"] == "Metric (latest)"


@pytest.mark.parametrize(
    ("mode", "search", "expected"),
    [
        (CompareMode.ALL, "", ["lr", "opt", "seed", "loss"]),
        (CompareMode.DIFF, "", ["opt", "seed", "loss"]),
        (CompareMode.ALL, "LR", ["lr"]),
        (CompareMode.ALL, "adam", ["opt"]),
        (CompareMode.DIFF, "lr", []),
        (CompareMode.ALL, "none-such", []),
    ],
)
def test_visible_rows_filter_by_mode_and_search(mode: CompareMode, search: str, expected: list[str]) -> None:
    assert [row["key"] for row in visible_rows(_rows(), mode, search)] == expected


def test_load_comparison_reads_the_picked_runs_latest_values_and_ignores_foreign_ones(
    store: SQLLiteStore, experiment_id: int
) -> None:
    project = store.create_project(models.NewProject(name="other", description="d"))
    other_experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    runs = [store.create_run(models.NewRun(experiment_id=experiment_id)) for _ in range(3)]
    foreign = store.create_run(models.NewRun(experiment_id=other_experiment.id))
    for run, lr in zip(runs, (0.1, 0.1, 0.3), strict=True):
        store.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment_id, {"lr": lr}))
    for step, loss in ((0, 2.0), (1, 1.0)):
        store.log_metrics(
            [
                models.LoggedMetrics(
                    metrics={"loss": loss},
                    step=step,
                    experiment_id=experiment_id,
                    run_id=runs[0].id,
                    timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
                )
            ]
        )

    compared, rows = load_comparison(store, experiment_id, [runs[2].id, foreign.id, runs[0].id])

    # Only this experiment's runs, in the order picked (the first is the baseline), and not the unpicked run.
    assert [run.id for run in compared] == [runs[2].id, runs[0].id]
    assert [(row["key"], row[run_field(runs[2].id)], row[run_field(runs[0].id)]) for row in rows] == [
        ("lr", 0.3, 0.1),
        ("loss", None, 1.0),
    ]


def test_load_comparison_copes_with_runs_that_logged_nothing(store: SQLLiteStore, experiment_id: int) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))

    assert load_comparison(store, experiment_id, []) == ([], [])
    compared, rows = load_comparison(store, experiment_id, [run.id])
    assert ([r.id for r in compared], rows) == ([run.id], [])
