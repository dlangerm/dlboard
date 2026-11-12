"""Tests for what a live-update poll tick fetches."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pendulum

from dlboard import models
from dlboard.models._view import ChartInstance, PanelInstance
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve._pages._experiment import _chart_view as chart_view
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment._live_updates import page_revision
from dlboard.serve._pages._experiment._page_edits import fetch_open_panel_dataframes
from dlboard.serve._pages._experiment._paging import Paging
from dlboard.serve._pages._experiment._shown_runs import page_experiments

if TYPE_CHECKING:
    import pytest

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

LineChart.register(allow_override=True)


def test_a_tick_fetches_only_the_charts_on_screen(
    store: SQLLiteStore, experiment_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    charts = [
        ChartInstance[Any, Any](chart_type="line", parameters={"column": f"m{i}", "x_axis": "step"})
        for i in range(20)
    ]
    page = store.get_or_create_page(page_model.BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(page.model_copy(update={"panels": [PanelInstance[Any, Any](name="p", charts=charts)]}))
    page = repo.load_page(store, repo.ExperimentRef(experiment_id, None))
    # The ids the browser reports back for the charts it shows -- JSON a callback reads as `ChartID`s.
    on_screen = [chart_view.ChartID.model_validate(chart_view.CHART_CONTENT.of("p", i)) for i in range(6, 9)]
    fetched: list[frozenset[str] | None] = []
    fetch_metrics = store.fetch_metrics

    def spy(experiment_id: int, **kwargs: Any) -> Any:  # noqa: ANN401
        fetched.append(kwargs["keys"])
        return fetch_metrics(experiment_id, **kwargs)

    monkeypatch.setattr(store, "fetch_metrics", spy)

    dataframes, ok = fetch_open_panel_dataframes(store, page, on_screen, Paging())

    assert ok
    assert set(dataframes) == {"p"}
    [keys] = fetched
    assert keys is not None
    assert {"m6", "m7", "m8"} <= keys
    assert not keys & {"m0", "m5", "m9", "m19"}


def test_a_comparison_polls_every_experiment_it_draws_and_reads_their_runs_but_the_hidden(
    store: SQLLiteStore,
) -> None:
    """A write to any of a comparison's experiments moves its revision on, and a tick reads each of them (#497)."""
    project_id = store.create_project(models.NewProject(name="p", description="")).id
    runs: list[models.Run] = []
    for _ in range(2):
        experiment_id = store.create_experiment(models.NewExperiment(project_id=project_id)).id
        run = store.create_run(models.NewRun(experiment_id=experiment_id))
        store.log_metrics(
            [
                models.LoggedMetrics(
                    metrics={"loss": 1.0},
                    step=0,
                    experiment_id=experiment_id,
                    run_id=run.id,
                    timestamp_utc=pendulum.now("UTC"),
                )
            ]
        )
        runs.append(run)
    chart = ChartInstance[Any, Any](chart_type="line", parameters={"column": "loss", "x_axis": "step"})
    comparison = cast(
        "page_model.BasicExperimentPage",
        store.create_comparison(
            page_model.BasicExperimentPage,
            models.NewPage[Any, Any](
                project_id=project_id,
                owner_id=store.get_or_create_user(models.Principal.unverified("alice")).id,
                name="c",
                panels=[PanelInstance[Any, Any](name="p", charts=[chart])],
            ),
            frozenset(run.id for run in runs),
        ),
    )

    def revision() -> str:
        return page_revision(
            [e for i in page_experiments(store, comparison) if (e := store.get_experiment(i))]
        )

    before = revision()
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"loss": 0.5},
                step=1,
                experiment_id=runs[1].experiment_id,
                run_id=runs[1].id,
                timestamp_utc=pendulum.now("UTC"),
            )
        ]
    )
    assert revision() != before

    on_screen = [chart_view.ChartID.model_validate(chart_view.CHART_CONTENT.of("p", 0))]
    every, ok = fetch_open_panel_dataframes(store, comparison, on_screen, Paging())
    assert ok
    assert set(every["p"].for_chart(chart).run_id) == {run.id for run in runs}
    unhidden, _ = fetch_open_panel_dataframes(
        store, comparison, on_screen, Paging(hidden=frozenset({runs[0].id}))
    )
    assert set(unhidden["p"].for_chart(chart).run_id) == {runs[1].id}
