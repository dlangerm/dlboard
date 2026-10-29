"""
What a comparison of several experiments asks the database for, counted in round trips.

`read_scaling_test.py` and the parity tests judge the rows a read returns. A comparison also pays a round trip for each
experiment it holds runs of, so a read that asks once per experiment, per panel or per keystroke is slow without
reading a row more.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pendulum
import pytest
import sqlalchemy as sa
from dash import Dash

from dlboard import models
from dlboard.models import ChartInstance, PanelInstance
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve._backend._app_slot import DASH_APP_EXTENSION
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _panel_data as panel_data
from dlboard.serve._pages._experiment import _panel_view as panel_view
from dlboard.serve._pages._experiment._column_catalog import COMPARISON_CATALOGS, ColumnCatalog
from dlboard.serve._pages._experiment._kept_reads import KeptReads
from dlboard.serve._pages._experiment._paging import Paging, visible_open_panels

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

LineChart.register(allow_override=True)

_TS = pendulum.datetime(2026, 1, 1, tz="UTC")
_EXPERIMENTS = 5
_PANELS = 4


def _log(store: SQLLiteStore, experiment_id: int, run_id: int, key: str) -> None:
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={key: 1.0}, step=0, experiment_id=experiment_id, run_id=run_id, timestamp_utc=_TS
            )
        ]
    )


@pytest.fixture
def comparison(store: SQLLiteStore) -> page_model.BasicExperimentPage:
    """A comparison of a run of each of `_EXPERIMENTS` experiments, with `_PANELS` panels of a chart each."""
    project_id = store.create_project(models.NewProject(name="p", description="d")).id
    runs: set[int] = set()
    for _ in range(_EXPERIMENTS):
        experiment_id = store.create_experiment(models.NewExperiment(project_id=project_id)).id
        run = store.create_run(models.NewRun(experiment_id=experiment_id))
        _log(store, experiment_id, run.id, "loss")
        runs.add(run.id)
    created = store.create_comparison(
        page_model.BasicExperimentPage,
        models.NewPage[Any, Any](
            project_id=project_id,
            owner_id=store.get_or_create_user(models.Principal.unverified("alice")).id,
            name="sweep",
            panels=[
                PanelInstance[Any, Any](
                    name=f"panel {index}",
                    charts=[
                        ChartInstance(
                            chart_type=LineChart.name, parameters={"column": "loss", "x_axis": "step"}
                        )
                    ],
                )
                for index in range(_PANELS)
            ],
        ),
        frozenset(runs),
    )
    return cast("page_model.BasicExperimentPage", created)


def _round_trips(store: SQLLiteStore, read: Callable[[], object]) -> int:
    statements: list[str] = []

    def record(_conn: sa.Connection, _cursor: object, statement: str, *_: object) -> None:
        if not statement.startswith("BEGIN"):  # a transaction opening is no round trip of a query
            statements.append(statement)

    sa.event.listen(store._engine, "before_cursor_execute", record)
    try:
        read()
    finally:
        sa.event.remove(store._engine, "before_cursor_execute", record)
    return len(statements)


@pytest.fixture
def app() -> Iterator[Dash]:
    """An app of its own, with the catalogs `ColumnCatalog.of_page` keeps, as a request to it has."""
    app = Dash(__name__)
    app.server.extensions[DASH_APP_EXTENSION] = app
    COMPARISON_CATALOGS.set(app, KeptReads(kept=8))
    with app.server.app_context():
        yield app


def test_the_experiments_names_are_read_once_for_a_page_and_not_for_each_of_its_panels(
    store: SQLLiteStore, comparison: page_model.BasicExperimentPage, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[int] = []
    get_experiment = store.get_experiment

    def counting(experiment_id: int) -> models.Experiment | None:
        asked.append(experiment_id)
        return get_experiment(experiment_id)

    monkeypatch.setattr(store, "get_experiment", counting)
    paging = panel_view.resolved_paging(
        comparison,
        Paging().viewing(tab="", open_panels=[p.name for p in comparison.panels]),
        focus_chart=None,
    )

    frames = panel_data.panel_frames(
        store, comparison, paging, visible_open_panels(comparison.panels, "", paging)
    )

    assert len(frames) == _PANELS
    assert len(asked) == _EXPERIMENTS


@pytest.mark.usefixtures("app")
def test_a_comparisons_catalog_is_read_again_only_after_a_write_to_one_of_its_experiments(
    store: SQLLiteStore, comparison: page_model.BasicExperimentPage
) -> None:
    """A key picker asks for it on every keystroke: kept, it costs a lookup of each experiment, not its key listings."""
    cold = _round_trips(store, lambda: ColumnCatalog.of_page(store, comparison))
    kept = _round_trips(store, lambda: ColumnCatalog.of_page(store, comparison))

    assert kept <= _EXPERIMENTS + 1  # the members, and a lookup of each experiment's revision
    assert cold > 3 * kept

    run = next(iter(store.comparison_runs(comparison.id)))
    _log(store, run.experiment_id, run.id, "accuracy")

    assert "accuracy" in ColumnCatalog.of_page(store, comparison).metrics
