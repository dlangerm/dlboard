"""Tests for the run-compare modal's URL state, row building, paging and loading."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pendulum
import pytest
from pydantic import ValidationError

from dlboard import models
from dlboard.serve._pages._experiment._run_compare import (
    COMPARE_PAGE_SIZE,
    MAX_COMPARED_RUNS,
    CompareIndex,
    CompareKey,
    CompareMode,
    ComparePage,
    CompareQuery,
    RowKind,
    comparison_page,
    comparison_rows,
    filtered_index,
    load_compared,
    load_run_compare,
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


def _index() -> CompareIndex:
    return CompareIndex.of([1, 2], _HPARAMS, _METRICS)


def test_the_index_keeps_each_keys_order_kind_and_whether_it_differs() -> None:
    assert _index().entries == [
        CompareKey("lr", RowKind.HPARAM, differs=False),
        CompareKey("opt", RowKind.HPARAM, differs=True),
        CompareKey("seed", RowKind.HPARAM, differs=True),
        CompareKey("loss", RowKind.METRIC, differs=True),
    ]


def test_the_index_is_what_a_page_request_sends_back_in_columns() -> None:
    sent = _index().model_dump()

    assert sent == {"keys": ["lr", "opt", "seed", "loss"], "hparams": 3, "differs": "0111"}
    assert CompareIndex.model_validate(sent) == _index()


@pytest.mark.parametrize(
    "sent",
    [
        {"keys": ["a"], "hparams": 0, "differs": ""},
        {"keys": ["a"], "hparams": 2, "differs": "1"},
        {"keys": ["a"], "hparams": 0, "differs": "x"},
    ],
    ids=["a-flag-missing", "more-hyperparameters-than-keys", "a-flag-that-is-neither"],
)
def test_an_index_the_browser_sends_back_wrong_is_refused(sent: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CompareIndex.model_validate(sent)


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
    assert [entry.key for entry in filtered_index(_index(), mode, search, _HPARAMS)] == expected


def test_load_run_compare_reads_the_picked_runs_latest_values_and_ignores_foreign_ones(
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

    comparison = load_run_compare(store, experiment_id, [runs[2].id, foreign.id, runs[0].id])

    # Only this experiment's runs, in the order picked (the first is the baseline), and not the unpicked run.
    assert [run.id for run in comparison.runs] == [runs[2].id, runs[0].id]
    assert comparison.index.entries == [
        CompareKey("lr", RowKind.HPARAM, differs=True),
        CompareKey("loss", RowKind.METRIC, differs=True),
    ]


def test_load_run_compare_copes_with_runs_that_logged_nothing(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))

    assert load_run_compare(store, experiment_id, []).runs == []
    comparison = load_run_compare(store, experiment_id, [run.id])
    assert ([r.id for r in comparison.runs], comparison.index.entries) == ([run.id], [])


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
    comparison = load_run_compare(store, experiment_id, [first.id, second.id])

    def page(number: int) -> ComparePage:
        return comparison_page(
            store,
            experiment_id,
            comparison.runs,
            comparison.hparams,
            comparison.index,
            CompareMode.DIFF,
            "",
            number,
        )

    last = page(2)

    assert [row["key"] for row in last.rows] == names[COMPARE_PAGE_SIZE:]
    assert last.rows[0][run_field(second.id)] == pytest.approx(COMPARE_PAGE_SIZE + 1.0)
    assert (last.matching, last.number, last.total) == (COMPARE_PAGE_SIZE + 1, 2, 2)
    # A page past the last one shows the last.
    assert page(9).number == 2


def test_a_page_of_no_runs_is_empty(store: SQLLiteStore, experiment_id: int) -> None:
    runs, hparams = load_compared(store, experiment_id, [])
    page = comparison_page(store, experiment_id, runs, hparams, CompareIndex(), CompareMode.ALL, "", 3)

    assert (page.rows, page.matching, page.number, page.total) == ([], 0, 1, 1)
