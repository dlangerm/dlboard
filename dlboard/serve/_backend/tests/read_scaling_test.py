"""
Reads that describe what was logged (which keys, where each run ended up, a summary of a series) must not cost more
because more was logged. A page that was instant in a run's first hour is slow on its tenth with either form of this:

- It reads a row per step trained: a history fetched to reduce in Python, a `SELECT DISTINCT` over every row.
- It reads the whole table to find one experiment's rows: an index that does not lead with the experiment, so
  every experiment ever logged is read to show one.

`query_plans_test.py` checks *how* a read reaches a table; this checks the outcome, whatever the query. Each
load below runs over an experiment that differs only in how long it trained, and over one whose neighbours
grew, and must cost about the same.

A chart that draws a series (a line chart) reads it, and that cost does grow with what was logged: it is not here.
That one is the logger's to bound, and `docs/client.md` says how.

A new page-level load that summarizes an experiment's metrics, artifacts or histograms belongs in `_LOADS`. If it
fails, it reads what was logged rather than what is shown: reduce it in the query (see CLAUDE.md, "Data flow").
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

import pandas as pd
import pendulum
import pytest
import sqlalchemy as sa

from dlboard import models
from dlboard.conftest import StoreBackend
from dlboard.models import ChartInstance, PanelInstance
from dlboard.plugins.charts.bar_chart import BarChart
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.serve._pages._experiment._column_catalog import ColumnCatalog
from dlboard.serve._pages._experiment._panel_data import fetch_panel_dataframe
from dlboard.serve._pages._experiment._run_compare import load_comparison

if TYPE_CHECKING:
    from collections.abc import Callable

    from dlboard.plugins.data_stores.postgres import PostgresStore

pytestmark = pytest.mark.postgres

BarChart.register(allow_override=True)
LineChart.register(allow_override=True)
TableChart.register(allow_override=True)

_SHORT, _LONG, _NEIGHBOUR = 10, 200, 1000
_RUNS = 3

_LINES = PanelInstance[pd.DataFrame, object](
    name="lines",
    charts=[ChartInstance(chart_type=LineChart.name, parameters={"column": "loss", "x_axis": "step"})],
)
_RUN_SUMMARIES = [
    PanelInstance[pd.DataFrame, object](
        name="bars",
        charts=[ChartInstance(chart_type=BarChart.name, parameters={"column": "loss", "x_axis": "acc"})],
    ),
    PanelInstance[pd.DataFrame, object](
        name="table",
        charts=[
            ChartInstance(
                chart_type=TableChart.name,
                parameters={"show_metrics": ["*"], "show_hparams": [], "aggregate": "mean"},
            )
        ],
    ),
]
_PATTERN_TABLE = PanelInstance[pd.DataFrame, object](
    name="pattern table",
    charts=[
        ChartInstance(
            chart_type=TableChart.name,
            parameters={"show_metrics": ["metric_*"], "show_hparams": [], "aggregate": "latest"},
        )
    ],
)
_LOADS: dict[str, Callable[[PostgresStore, int], object]] = {
    "the latest of a metric pattern": lambda s, e: fetch_panel_dataframe(s, e, _PATTERN_TABLE, run_ids=None),
    "the column catalog": lambda s, e: ColumnCatalog.load(s, e),
    "metric keys": lambda s, e: s.summarize_metric_keys(e),
    "histogram keys": lambda s, e: s.summarize_histogram_keys(e),
    "artifact keys": lambda s, e: s.summarize_artifact_keys(e),
    "latest metrics": lambda s, e: s.fetch_metrics(
        e, reduction=models.MetricReduction.LATEST, run_ids=None
    ).latest_per_run(),
    "the newest runs": lambda s, e: list(s.get_runs(e, limit=_SHOWN, run_ids=None)),
    "hyperparameters of every run": lambda s, e: list(s.fetch_hyperparams(e, run_ids=None)),
    "the run comparison": lambda s, e: load_comparison(s, e, [run.id for run in s.get_runs(e, run_ids=None)]),
    "a panel of bars of the latest": lambda s, e: fetch_panel_dataframe(
        s, e, _RUN_SUMMARIES[0], run_ids=None
    ),
    "a table of every metric's mean": lambda s, e: fetch_panel_dataframe(
        s, e, _RUN_SUMMARIES[1], run_ids=None
    ),
}


_SHOWN = 3
"""How many of an experiment's newest runs a page is drawing."""
_SHOWN_LOADS: dict[str, Callable[[PostgresStore, int, frozenset[int]], object]] = {
    "latest metrics": lambda s, e, shown: s.fetch_metrics(
        e, run_ids=shown, reduction=models.MetricReduction.LATEST
    ).latest_per_run(),
    "hyperparameters of the shown runs": lambda s, e, shown: list(s.fetch_hyperparams(e, run_ids=shown)),
    "the run comparison": lambda s, e, shown: load_comparison(s, e, sorted(shown)),
    "a panel of bars of the latest": lambda s, e, shown: fetch_panel_dataframe(
        s, e, _RUN_SUMMARIES[0], shown
    ),
    "a table of every metric's mean": lambda s, e, shown: fetch_panel_dataframe(
        s, e, _RUN_SUMMARIES[1], shown
    ),
    "a panel of lines": lambda s, e, shown: fetch_panel_dataframe(s, e, _LINES, shown),
}


class _Work(NamedTuple):
    rows: int
    """What the database sent back."""
    pages: int
    """What it read to do so."""


@pytest.fixture
def store_backend() -> StoreBackend:
    return StoreBackend.POSTGRES


def _train(store: PostgresStore, project_id: int, steps: int, runs: int = _RUNS, keys: int = 0) -> int:
    """
    A new experiment whose `runs` runs logged two metrics (and `keys` more), a histogram and an image at each step.
    """
    experiment = store.create_experiment(models.NewExperiment(project_id=project_id))
    for _ in range(runs):
        run = store.create_run(models.NewRun(experiment_id=experiment.id))
        store.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment.id, {"lr": 0.1}))
        store.log_metrics(
            models.LoggedMetrics(
                metrics={
                    "loss": 1.0 / (step + 1),
                    "acc": float(step),
                    **{f"metric_{i:04d}": float(step + i) for i in range(keys)},
                },
                histograms={"grad": models.Histogram(edges=[0.0, 1.0], counts=[step])},
                step=step,
                experiment_id=experiment.id,
                run_id=run.id,
                timestamp_utc=pendulum.now(),
            )
            for step in range(steps)
        )
        store.log_artifact_refs(
            models.Artifact(
                key="img",
                fname="img.png",
                run_id=run.id,
                experiment_id=experiment.id,
                step=step,
                ref=f"ref://{run.id}/{step}",
            )
            for step in range(steps)
        )
    with store._engine.begin() as conn:
        # What the planner chooses between a scan and an index on, as autovacuum would have it by the time anyone looks.
        for table in store.tables.values():
            conn.exec_driver_sql(f'ANALYZE "{table.schema}"."{table.name}"')
    return experiment.id


def _work(store: PostgresStore, load: Callable[[], object]) -> _Work:
    """What running `load` cost: the rows its queries returned, and the pages they read, rerun under `EXPLAIN`."""
    selects: list[tuple[str, Any, int]] = []

    def record(_conn: sa.Connection, cursor: Any, statement: str, parameters: Any, *_: object) -> None:  # noqa: ANN401
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            selects.append((statement, parameters, cursor.rowcount))

    sa.event.listen(store._engine, "after_cursor_execute", record)
    try:
        load()
    finally:
        sa.event.remove(store._engine, "after_cursor_execute", record)
    pages = 0
    with store._engine.connect() as conn:
        for statement, parameters, _ in selects:
            ((document,),) = conn.exec_driver_sql(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters
            )
            root: dict[str, int] = document[0]["Plan"]
            pages += root["Shared Hit Blocks"] + root["Shared Read Blocks"]
    return _Work(sum(rows for _, _, rows in selects), pages)


@pytest.mark.parametrize("name", _LOADS)
def test_a_load_reads_no_more_rows_for_an_experiment_that_trained_longer(
    store: PostgresStore, name: str
) -> None:
    project_id = store.create_project(models.NewProject(name="p", description="d")).id
    short, long = _train(store, project_id, _SHORT), _train(store, project_id, _LONG)

    read_short = _work(store, lambda: _LOADS[name](store, short)).rows
    read_long = _work(store, lambda: _LOADS[name](store, long)).rows

    assert read_short > 0
    assert read_long <= 2 * read_short, f"{read_long} rows for {_LONG} steps, {read_short} for {_SHORT}"


@pytest.mark.parametrize("name", _LOADS)
def test_a_load_reads_no_more_pages_when_another_experiment_grows(store: PostgresStore, name: str) -> None:
    project_id = store.create_project(models.NewProject(name="p", description="d")).id
    experiment = _train(store, project_id, _SHORT)
    alone = _work(store, lambda: _LOADS[name](store, experiment)).pages

    _train(store, project_id, _NEIGHBOUR)
    beside_it = _work(store, lambda: _LOADS[name](store, experiment)).pages

    assert beside_it <= 2 * alone + 10, (
        f"{beside_it} pages beside a {_NEIGHBOUR}-step experiment, {alone} alone"
    )


@pytest.mark.parametrize("name", _SHOWN_LOADS)
def test_a_load_of_the_runs_shown_reads_no_more_for_an_experiment_of_more_runs(
    store: PostgresStore, name: str
) -> None:
    """Runs are what an experiment can have thousands of, and a page draws only the few it is told to."""
    project_id = store.create_project(models.NewProject(name="p", description="d")).id
    few, many = _train(store, project_id, _SHORT, runs=_SHOWN), _train(store, project_id, _SHORT, runs=60)

    def of_the_newest(experiment_id: int) -> _Work:
        shown = frozenset(run.id for run in store.get_runs(experiment_id, limit=_SHOWN, run_ids=None))
        return _work(store, lambda: _SHOWN_LOADS[name](store, experiment_id, shown))

    alone, among_many = of_the_newest(few), of_the_newest(many)

    assert alone.rows > 0
    assert among_many.rows <= 2 * alone.rows, (
        f"{among_many.rows} rows beside 60 runs, {alone.rows} for {_SHOWN}"
    )
    assert among_many.pages <= 2 * alone.pages + 10, f"{among_many.pages} pages beside 60 runs, {alone.pages}"


_KEY_SUMMARIES: dict[str, Callable[[PostgresStore, int], object]] = {
    "metric steps": lambda s, e: s.summarize_metric_steps(e, frozenset(s.summarize_metric_keys(e))),
    "the column catalog": lambda s, e: ColumnCatalog.load(s, e),
}


@pytest.mark.parametrize("name", _KEY_SUMMARIES)
def test_a_key_summary_reads_no_more_once_it_judges_only_the_newest_runs(
    store: PostgresStore, name: str
) -> None:
    """A key summary judges each key on the newest runs (`SUMMARY_RUNS`), so past that many runs it reads the same."""
    project_id = store.create_project(models.NewProject(name="p", description="d")).id
    few, many = _train(store, project_id, _SHORT, runs=120), _train(store, project_id, _SHORT, runs=300)

    alone = _work(store, lambda: _KEY_SUMMARIES[name](store, few))
    among_many = _work(store, lambda: _KEY_SUMMARIES[name](store, many))

    assert alone.rows > 0
    assert among_many.rows <= 2 * alone.rows, f"{among_many.rows} rows for 300 runs, {alone.rows} for 120"
    assert among_many.pages <= 2 * alone.pages + 10, (
        f"{among_many.pages} pages for 300 runs, {alone.pages} for 120"
    )


_WIDE_KEYS = 600
"""More keys than SQLite caps a compound SELECT at, so a load that reads key by key is also measured past that cap."""
_WIDE_LONG = 50
"""The steps the long experiment trains when it has `_WIDE_KEYS` keys: its rows grow with the keys and the steps."""
_KNOWN_GROWTH: dict[str, str] = {
    "a table of every metric's mean": "#393: a MIN, MAX or MEAN reads every step of every key it reduces",
}
"""
Loads that read more as a run trains, each an expected failure until its issue is fixed. Fixing one must remove its
entry: the strict xfail then reports it.
"""


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(name, marks=pytest.mark.xfail(strict=True, reason=_KNOWN_GROWTH[name]))
        if name in _KNOWN_GROWTH
        else name
        for name in _LOADS
    ],
)
def test_a_load_over_many_keys_reads_no_more_pages_for_an_experiment_that_trained_longer(
    store: PostgresStore, name: str
) -> None:
    """Past many keys, a load must still read by the key and by the run, not by each step a run trained."""
    project_id = store.create_project(models.NewProject(name="p", description="d")).id
    short = _train(store, project_id, _SHORT, keys=_WIDE_KEYS)
    long = _train(store, project_id, _WIDE_LONG, keys=_WIDE_KEYS)

    read_short = _work(store, lambda: _LOADS[name](store, short))
    read_long = _work(store, lambda: _LOADS[name](store, long))

    assert read_short.rows > 0
    assert read_long.pages <= 2 * read_short.pages + 10, (
        f"{read_long.pages} pages for {_WIDE_LONG} steps, {read_short.pages} for {_SHORT}, with {_WIDE_KEYS} keys"
    )
