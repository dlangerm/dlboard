"""Tests for the run-compare modal's URL state, row building, paging and loading."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pendulum
import pytest

from dlboard import models
from dlboard.serve._pages._experiment._run_compare import (
    COMPARE_PAGE_SIZE,
    MAX_COMPARED_RUNS,
    CompareMode,
    CompareQuery,
    comparison_index,
    comparison_page,
    comparison_rows,
    filtered_index,
    load_comparison,
    run_field,
)

if TYPE_CHECKING:
    from dlboard.models import FlatHparamDict
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

_HPARAMS: dict[int, FlatHparamDict] = {1: {"lr": 0.1, "opt": "adam", "seed": 0}, 2: {"lr": 0.1, "opt": "sgd"}}
_METRICS = {1: {"loss": 0.5}, 2: {"loss": 0.4}}


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


@pytest.mark.parametrize(
    ("query", "page"),
    [({}, 1), ({"compare": "3", "compare_page": "4"}, 4), ({"compare": "3", "compare_page": "x"}, 1)],
)
def test_compare_query_reads_the_page(query: dict[str, str], page: int) -> None:
    assert CompareQuery.from_query(query).page == page


def _rows() -> list[dict[str, Any]]:
    return comparison_rows([1, 2], hparams=_HPARAMS, metrics=_METRICS)


def test_comparison_rows_list_hparams_then_metrics_and_flag_changes_from_the_baseline() -> None:
    rows = {row["key"]: row for row in _rows()}

    assert [row["key"] for row in _rows()] == ["lr", "opt", "seed", "loss"]
    assert rows["lr"]["_changed"] == []
    assert rows["opt"]["_changed"] == [run_field(2)]
    # A key only some runs logged counts as a difference.
    assert rows["seed"][run_field(2)] is None
    assert rows["seed"]["_differs"]
    assert rows["loss"]["kind"] == "Metric (latest)"


def test_comparison_index_keeps_each_keys_order_and_whether_it_differs() -> None:
    assert comparison_index(_rows()) == [
        {"key": "lr", "kind": "Hyperparameter", "_differs": False},
        {"key": "opt", "kind": "Hyperparameter", "_differs": True},
        {"key": "seed", "kind": "Hyperparameter", "_differs": True},
        {"key": "loss", "kind": "Metric (latest)", "_differs": True},
    ]


@pytest.mark.parametrize(
    ("mode", "search", "expected"),
    [
        (CompareMode.ALL, "", ["lr", "opt", "seed", "loss"]),
        (CompareMode.DIFF, "", ["opt", "seed", "loss"]),
        (CompareMode.ALL, "LR", ["lr"]),
        (CompareMode.ALL, "adam", ["opt"]),
        (CompareMode.ALL, "0", ["lr", "seed"]),
        # A metric's values are not searched, only its name.
        (CompareMode.ALL, "0.4", []),
        (CompareMode.DIFF, "lr", []),
        (CompareMode.ALL, "none-such", []),
    ],
)
def test_filtered_index_filters_by_mode_and_search(
    mode: CompareMode, search: str, expected: list[str]
) -> None:
    index = comparison_index(_rows())

    assert [entry["key"] for entry in filtered_index(index, mode, search, _HPARAMS)] == expected


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
                    timestamp_utc=pendulum.datetime(2026, 1, 1, tz="UTC"),
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


def test_a_page_lists_its_keys_and_reads_only_their_values(store: SQLLiteStore, experiment_id: int) -> None:
    first, second = (store.create_run(models.NewRun(experiment_id=experiment_id)) for _ in range(2))
    names = [f"metric_{i:03d}" for i in range(COMPARE_PAGE_SIZE + 1)]
    for run, offset in ((first, 0.0), (second, 1.0)):
        store.log_metrics(
            [
                models.LoggedMetrics(
                    metrics={name: i + offset for i, name in enumerate(names)},
                    step=0,
                    experiment_id=experiment_id,
                    run_id=run.id,
                    timestamp_utc=pendulum.datetime(2026, 1, 1, tz="UTC"),
                )
            ]
        )
    run_ids = [first.id, second.id]
    _, rows = load_comparison(store, experiment_id, run_ids)
    index = comparison_index(rows)

    last = comparison_page(store, experiment_id, run_ids, index, CompareMode.DIFF, "", 2)

    assert [row["key"] for row in last.rows] == names[COMPARE_PAGE_SIZE:]
    assert last.rows[0][run_field(second.id)] == pytest.approx(COMPARE_PAGE_SIZE + 1.0)
    assert (last.matching, last.number, last.total) == (COMPARE_PAGE_SIZE + 1, 2, 2)
    # A page past the last one shows the last.
    assert comparison_page(store, experiment_id, run_ids, index, CompareMode.DIFF, "", 9).number == 2


def test_a_page_of_no_runs_is_empty(store: SQLLiteStore, experiment_id: int) -> None:
    page = comparison_page(store, experiment_id, [], [], CompareMode.ALL, "", 3)

    assert (page.rows, page.matching, page.number, page.total) == ([], 0, 1, 1)
