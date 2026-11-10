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
from dlboard.conftest import Clock, StoreBackend
from dlboard.models import ChartInstance, PanelInstance
from dlboard.plugins.charts.bar_chart import BarChart
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.series_band import SeriesBandChart
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.plugins.retention._candidates import Candidates, Rules
from dlboard.plugins.retention._rules import RetentionRule, RetentionUnit, RunAge, RunCount
from dlboard.plugins.retention._settings import RetentionSettings
from dlboard.serve._pages._experiment._column_catalog import ColumnCatalog, summary_hparam_keys
from dlboard.serve._pages._experiment._panel_data import fetch_panel_data, scope_panel_data
from dlboard.serve._pages._experiment._run_compare import load_run_compare

if TYPE_CHECKING:
    from collections.abc import Callable

    from dlboard.plugins.data_stores.postgres import PostgresStore

pytestmark = pytest.mark.postgres

BarChart.register(allow_override=True)
LineChart.register(allow_override=True)
SeriesBandChart.register(allow_override=True)
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
_BAND = PanelInstance[pd.DataFrame, object](
    name="band",
    # A band is a probe of the index for each key, run and cell, and a probe costs the depth of the index, which a
    # neighbour of thousands of rows deepens a little. A few probes keep that under this test's slack, as a scan is not.
    charts=[
        ChartInstance(chart_type=SeriesBandChart.name, parameters={"metrics": ["*"], "grid": 4, "runs": 1})
    ],
)
_LOADS: dict[str, Callable[[PostgresStore, int], object]] = {
    "the band of every metric": lambda s, e: fetch_panel_data(s, e, _BAND, run_ids=None),
    "the latest of a metric pattern": lambda s, e: fetch_panel_data(s, e, _PATTERN_TABLE, run_ids=None),
    "the column catalog": lambda s, e: ColumnCatalog.load(s, e),
    "metric keys": lambda s, e: s.summarize_metric_keys(e),
    "histogram keys": lambda s, e: s.summarize_histogram_keys(e),
    "artifact keys": lambda s, e: s.summarize_artifact_keys(e),
    "latest metrics": lambda s, e: s.fetch_metrics(
        e, reduction=models.MetricReduction.LATEST, run_ids=None
    ).latest_per_run(),
    "the newest runs": lambda s, e: list(s.get_runs(e, limit=_SHOWN, run_ids=None)),
    "hyperparameters of every run": lambda s, e: list(s.fetch_hyperparams(e, run_ids=None)),
    "the run compare": lambda s, e: load_run_compare(
        s,
        {e: frozenset(run.id for run in s.get_runs(e, run_ids=None))},
        [run.id for run in s.get_runs(e, run_ids=None)],
    ),
    "a panel of bars of the latest": lambda s, e: fetch_panel_data(s, e, _RUN_SUMMARIES[0], run_ids=None),
    "a table of every metric's mean": lambda s, e: fetch_panel_data(s, e, _RUN_SUMMARIES[1], run_ids=None),
}


_SHOWN = 3
"""How many of an experiment's newest runs a page is drawing."""
_SHOWN_LOADS: dict[str, Callable[[PostgresStore, int, frozenset[int]], object]] = {
    "latest metrics": lambda s, e, shown: s.fetch_metrics(
        e, run_ids=shown, reduction=models.MetricReduction.LATEST
    ).latest_per_run(),
    "hyperparameters of the shown runs": lambda s, e, shown: list(s.fetch_hyperparams(e, run_ids=shown)),
    "the run compare": lambda s, e, shown: load_run_compare(s, {e: shown}, sorted(shown)),
    "a panel of bars of the latest": lambda s, e, shown: fetch_panel_data(s, e, _RUN_SUMMARIES[0], shown),
    "a table of every metric's mean": lambda s, e, shown: fetch_panel_data(s, e, _RUN_SUMMARIES[1], shown),
    "a panel of lines": lambda s, e, shown: fetch_panel_data(s, e, _LINES, shown),
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


def _work(store: PostgresStore, load: Callable[[], object], *, scans: bool = True) -> _Work:
    """
    What running `load` cost: the rows its queries returned, and the pages they read, rerun under `EXPLAIN`.

    Without `scans`, a read of a table that has an index to use must use it: tables this small are cheaper to scan, which
    says nothing of the plan at a size where a scan is what a read must not do.
    """
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
        if not scans:
            conn.exec_driver_sql("SET enable_seqscan = off")
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


def _analyzed(store: PostgresStore) -> None:
    """What the planner chooses between a scan and an index on, as autovacuum would have it by the time anyone looks."""
    with store._engine.begin() as conn:
        for table in store.tables.values():
            conn.exec_driver_sql(f'ANALYZE "{table.schema}"."{table.name}"')


def test_a_projects_pins_read_no_more_when_its_own_runs_or_another_projects_pins_grow(
    store: PostgresStore,
) -> None:
    """The pins listed are what a read of them costs: not how many runs the project has, nor how many pins any other has."""
    me = store.get_or_create_user(models.Principal.unverified("pinner")).id
    project_id = store.create_project(models.NewProject(name="mine", description="")).id
    experiment = store.create_experiment(models.NewExperiment(project_id=project_id)).id
    pinned = frozenset(store.create_run(models.NewRun(experiment_id=experiment)).id for _ in range(_SHOWN))
    store.pin_runs(me, pinned, models.PinColor.RED)
    _analyzed(store)

    def reads() -> _Work:
        return _work(
            store,
            lambda: (
                store.list_pinned_runs(project_id),
                store.pin_counts(project_id),
                store.pinned_run_ids(project_id, limit=100),
                store.list_pinners(project_id),
            ),
            scans=False,
        )

    alone = reads()
    # The project's runs grow by thousands, and so do another project's pins.
    crowded = store.create_experiment(models.NewExperiment(project_id=project_id)).id
    for _ in range(1500):
        store.create_run(models.NewRun(experiment_id=crowded))
    other_project = store.create_project(models.NewProject(name="other", description="")).id
    for _ in range(50):  # pins are spread over a project's experiments, as people make them
        elsewhere = store.create_experiment(models.NewExperiment(project_id=other_project)).id
        others = frozenset(store.create_run(models.NewRun(experiment_id=elsewhere)).id for _ in range(30))
        store.pin_runs(me, others, models.PinColor.BLUE)
    _analyzed(store)
    grown = reads()

    assert alone.rows > 0
    assert grown.rows <= 2 * alone.rows, (
        f"{grown.rows} rows beside 3,000 more runs and 1,500 more pins, {alone.rows}"
    )
    assert grown.pages <= 2 * alone.pages + 10, f"{grown.pages} pages beside them, {alone.pages}"


def test_a_page_of_a_projects_cards_reads_no_more_when_other_projects_or_its_unshown_experiments_grow(
    store: PostgresStore,
) -> None:
    """
    The project page reads its newest experiments and their stats, a page of them (#502).

    Not every experiment of every project, nor the runs of the experiments it is not showing. How many experiments the
    project has is counted for the pager, from the project's own index entries, so it is the project's own growth that
    is held still here.
    """
    me = store.get_or_create_user(models.Principal.unverified("viewer")).id
    project_id = store.create_project(models.NewProject(name="mine", description="")).id
    oldest = [store.create_experiment(models.NewExperiment(project_id=project_id)).id for _ in range(10)]
    for _ in range(24):
        store.create_experiment(models.NewExperiment(project_id=project_id))
    _analyzed(store)

    def reads() -> _Work:
        def cards() -> None:
            shown = store.search_experiments(project_id, limit=24)
            store.get_experiment_stats(project_id, experiment_ids=[e.id for e in shown.experiments])
            store.search_comparisons(project_id, me, limit=24)

        return _work(store, cards, scans=False)

    alone = reads()
    # Another project grows by a thousand experiments, and this project's oldest (not on the first page) by many runs.
    other_project = store.create_project(models.NewProject(name="other", description="")).id
    for _ in range(1000):
        store.create_experiment(models.NewExperiment(project_id=other_project))
    for experiment in oldest:
        for _ in range(150):
            store.create_run(models.NewRun(experiment_id=experiment))
    _analyzed(store)
    grown = reads()

    assert alone.rows > 0
    assert grown.rows <= 2 * alone.rows, (
        f"{grown.rows} rows beside 1,000 more experiments and 1,500 runs, {alone.rows}"
    )
    assert grown.pages <= 2 * alone.pages + 10, f"{grown.pages} pages beside them, {alone.pages}"


def test_a_comparison_of_two_experiments_reads_no_more_when_each_has_more_runs(store: PostgresStore) -> None:
    """A comparison draws the runs it holds of each experiment; what else they hold is none of its business."""
    project_id = store.create_project(models.NewProject(name="p", description="d")).id
    few = _train(store, project_id, _SHORT, runs=_SHOWN), _train(store, project_id, _SHORT, runs=_SHOWN)
    many = _train(store, project_id, _SHORT, runs=60), _train(store, project_id, _SHORT, runs=60)

    def of_the_newest(experiments: tuple[int, int]) -> _Work:
        scope = {
            experiment_id: frozenset(
                run.id for run in store.get_runs(experiment_id, limit=_SHOWN, run_ids=None)
            )
            for experiment_id in experiments
        }
        return _work(store, lambda: scope_panel_data(store, scope, _LINES))

    alone, among_many = of_the_newest(few), of_the_newest(many)

    assert alone.rows > 0
    assert among_many.rows <= 2 * alone.rows, f"{among_many.rows} rows beside 60 runs each, {alone.rows}"
    assert among_many.pages <= 2 * alone.pages + 10, f"{among_many.pages} pages beside 60 runs, {alone.pages}"


_KEY_SUMMARIES: dict[str, Callable[[PostgresStore, int], object]] = {
    "metric steps": lambda s, e: s.summarize_metric_steps(e, frozenset(s.summarize_metric_keys(e))),
    "the column catalog": lambda s, e: ColumnCatalog.load(s, e),
    "the Columns picker's hyperparameter keys": lambda s, e: summary_hparam_keys(s, e),
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


def _stray_only_in_a_deleted_run(store: PostgresStore, project_id: int, runs: int) -> int:
    """
    An experiment of `runs` live runs, each logging `loss` at 20 steps; one more run, soft-deleted, is the only one to log `stray`.

    No live run logged `stray`, so the catalog must look at every live run to find that out (#372).
    """
    experiment = store.create_experiment(models.NewExperiment(project_id=project_id))
    for _ in range(runs):
        run = store.create_run(models.NewRun(experiment_id=experiment.id))
        store.log_metrics(
            [
                models.LoggedMetrics(
                    metrics={"loss": float(step)},
                    step=step,
                    experiment_id=experiment.id,
                    run_id=run.id,
                    timestamp_utc=pendulum.now(),
                )
                for step in range(20)
            ]
        )
    gone = store.create_run(models.NewRun(experiment_id=experiment.id))
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"stray": 1.0},
                step=step,
                experiment_id=experiment.id,
                run_id=gone.id,
                timestamp_utc=pendulum.now(),
            )
            for step in range(20)
        ]
    )
    store.delete_run(gone.id, store.get_or_create_user(models.Principal.unverified("admin")))
    with store._engine.begin() as conn:
        for table in store.tables.values():
            conn.exec_driver_sql(f'ANALYZE "{table.schema}"."{table.name}"')
    return experiment.id


def test_the_key_check_reads_no_more_for_a_key_only_a_deleted_run_logged_among_more_live_runs(
    store: PostgresStore,
) -> None:
    """
    A key only a deleted run logged is not listed, and the listing returns no more rows as the live runs grow (#372).

    Pages are not asserted: the plan Postgres picks for the probe varies with its statistics, and a sequential scan
    of a small table costs little more than an index scan, so a page bound flakes without a user-visible difference.
    """
    project_id = store.create_project(models.NewProject(name="p", description="d")).id
    few = _stray_only_in_a_deleted_run(store, project_id, runs=100)
    many = _stray_only_in_a_deleted_run(store, project_id, runs=1000)

    alone = _work(store, lambda: store.summarize_metric_keys(few))
    among_many = _work(store, lambda: store.summarize_metric_keys(many))

    assert store.summarize_metric_keys(few) == ["loss"]
    assert store.summarize_metric_keys(many) == ["loss"]
    assert alone.rows == among_many.rows


@pytest.mark.parametrize(
    "rule",
    [
        RunCount(max_runs=20),
        RunCount(max_runs=20, unit=RetentionUnit.PROJECT),
        RunAge(max_age_days=5),
    ],
    ids=["each experiment's runs", "the project's runs", "age"],
)
def test_finding_what_retention_deletes_reads_no_more_for_what_it_deleted_already(
    store: PostgresStore, rule: RetentionRule, clock: Clock
) -> None:
    """
    What a rule reads is what it keeps, not the Trash it has filled from the oldest end.

    Another project's runs are there from the start: they deepen the indexes every probe descends, a page or two each,
    which is not what this is about (`test_a_load_reads_no_more_pages_when_another_experiment_grows` is).
    """
    admin = store.get_or_create_user(models.Principal.unverified("admin"))
    project_id = store.create_project(models.NewProject(name="mine", description="")).id
    experiment = store.create_experiment(models.NewExperiment(project_id=project_id)).id
    now = pendulum.now("UTC")

    def log(count: int, days_ago: int, into: int = experiment) -> list[int]:
        return [
            store.create_run(
                models.NewRun(
                    experiment_id=into,
                    status=models.RunStatus.FINISHED,
                    created_at=now.subtract(days=days_ago, seconds=i),
                )
            ).id
            for i in range(count)
        ]

    other = store.create_experiment(
        models.NewExperiment(
            project_id=store.create_project(models.NewProject(name="other", description="")).id
        )
    ).id
    log(1500, days_ago=20, into=other)
    log(25, days_ago=1)
    log(10, days_ago=9)
    _analyzed(store)
    candidates = Candidates(store, RetentionSettings(), now)

    def reads() -> _Work:
        return _work(
            store,
            lambda: candidates.find(project_id, Rules(project=rule, experiments={}), limit=100),
            scans=False,
        )

    alone = reads()
    store.delete_runs(frozenset(log(1500, days_ago=20)), admin)
    _analyzed(store)
    run_table = store.tables[models.Run]
    with store._engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as conn:  # as autovacuum would have
        conn.exec_driver_sql(f'VACUUM "{run_table.schema}"."{run_table.name}"')
    grown = reads()

    assert alone.rows > 0
    assert grown.rows <= 2 * alone.rows, f"{grown.rows} rows beside 1,500 runs in the Trash, {alone.rows}"
    assert grown.pages <= 2 * alone.pages + 10, f"{grown.pages} pages beside them, {alone.pages}"


def test_a_retention_batch_reads_about_its_limit_and_a_backlog_is_counted_not_read(
    store: PostgresStore, clock: Clock
) -> None:
    """
    A project of many experiments, each far past its rule: a batch reads what it deletes, and the backlog costs a count.

    What is read for every experiment is where its rule cuts it (the count and the slack: what the project keeps). The
    runs to delete are read from one experiment at a time, until the batch is full, and the backlog is never read.
    """
    project_id = store.create_project(models.NewProject(name="wide", description="")).id
    experiments = [store.create_experiment(models.NewExperiment(project_id=project_id)).id for _ in range(25)]
    now = pendulum.now("UTC")
    for experiment in experiments:
        for i in range(120):
            store.create_run(
                models.NewRun(
                    experiment_id=experiment,
                    status=models.RunStatus.FINISHED,
                    created_at=now.subtract(days=1, seconds=i),
                )
            )
    _analyzed(store)
    rules = Rules(project=RunCount(max_runs=20), experiments={})
    candidates = Candidates(store, RetentionSettings(), now)

    batch = _work(store, lambda: candidates.find(project_id, rules, limit=100), scans=False)
    backlog = _work(store, lambda: candidates.overdue(project_id, rules, limit=10_000), scans=False)

    assert candidates.overdue(project_id, rules, limit=10_000) == 25 * 100
    cut = 25 * (20 + 10 + 1)  # each experiment's newest, to its slack: what its rule keeps
    assert batch.rows <= cut + 100 + 50, f"{batch.rows} rows for a batch of 100 across 25 experiments"
    assert backlog.rows <= cut + 2 * 25, (
        f"{backlog.rows} rows to count 2,500 runs to delete"
    )  # + the experiments, the counts
