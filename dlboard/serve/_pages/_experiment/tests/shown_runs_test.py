"""Which runs a page draws: the ones a view selected, else only the newest few, whatever an experiment holds."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pendulum

from dlboard import models
from dlboard.models import ChartInstance, PanelInstance
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment import _panel_data as panel_data
from dlboard.serve._pages._experiment import _panel_view as panel_view
from dlboard.serve._pages._experiment._page_model import DocumentSettings
from dlboard.serve._pages._experiment._paging import Paging, visible_open_panels
from dlboard.serve._pages._experiment._shown_runs import shown_run_ids

if TYPE_CHECKING:
    import pytest

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

LineChart.register(allow_override=True)

_TS = pendulum.datetime(2026, 1, 1, tz="UTC")


def _runs(store: SQLLiteStore, experiment_id: int, count: int) -> list[models.Run]:
    """`count` runs that each logged a loss, oldest first."""
    runs = [store.create_run(models.NewRun(experiment_id=experiment_id)) for _ in range(count)]
    store.log_metrics(
        models.LoggedMetrics(
            metrics={"loss": 1.0}, step=0, experiment_id=experiment_id, run_id=run.id, timestamp_utc=_TS
        )
        for run in runs
    )
    return runs


def test_a_page_draws_the_newest_twenty_runs_until_told_otherwise(
    store: SQLLiteStore, experiment_id: int
) -> None:
    runs = _runs(store, experiment_id, 25)

    assert shown_run_ids(store, experiment_id, DocumentSettings()) == {run.id for run in runs[-20:]}


def test_how_many_runs_a_page_draws_by_default_is_configurable(
    store: SQLLiteStore, experiment_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _runs(store, experiment_id, 5)
    monkeypatch.setenv("DLBOARD_DEFAULT_SHOWN_RUNS", "2")

    assert shown_run_ids(store, experiment_id, DocumentSettings()) == {run.id for run in runs[-2:]}


def test_a_selection_is_drawn_whatever_its_age_and_an_empty_one_draws_nothing(
    store: SQLLiteStore, experiment_id: int
) -> None:
    runs = _runs(store, experiment_id, 25)

    assert shown_run_ids(store, experiment_id, DocumentSettings(selected_runs=[runs[0].id, runs[3].id])) == {
        runs[0].id,
        runs[3].id,
    }
    assert shown_run_ids(store, experiment_id, DocumentSettings(selected_runs=[])) == frozenset()


def test_a_view_saved_with_excluded_runs_draws_the_newest_it_did_not_leave_out(
    store: SQLLiteStore, experiment_id: int
) -> None:
    runs = _runs(store, experiment_id, 25)
    document = DocumentSettings(excluded_runs=[runs[-1].id, runs[0].id])

    assert shown_run_ids(store, experiment_id, document) == {run.id for run in runs[-21:-1]}


def test_a_view_that_kept_a_few_runs_still_draws_them(store: SQLLiteStore, experiment_id: int) -> None:
    """Saved as "all runs but these two": leaving out every newer run must not blank the page."""
    runs = _runs(store, experiment_id, 50)
    document = DocumentSettings(excluded_runs=[run.id for run in runs[2:]])

    assert shown_run_ids(store, experiment_id, document) == {runs[0].id, runs[1].id}


def test_a_deleted_run_is_not_one_of_the_newest(store: SQLLiteStore, experiment_id: int) -> None:
    runs = _runs(store, experiment_id, 4)
    store.delete_run(runs[-1].id, store.get_or_create_user(models.Principal.unverified("alice")))

    assert runs[-1].id not in shown_run_ids(store, experiment_id, DocumentSettings())


def test_the_charts_of_an_experiment_of_many_runs_are_drawn_from_only_the_newest(
    store: SQLLiteStore, experiment_id: int
) -> None:
    runs = _runs(store, experiment_id, 45)
    panel = PanelInstance[Any, Any](
        name="p", charts=[ChartInstance(chart_type="line", parameters={"column": "loss", "x_axis": "step"})]
    )
    page = store.get_or_create_page(page_model.BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(page.model_copy(update={"panels": [panel]}))
    loaded = repo.load_page(store, repo.PageRef(experiment_id, None))
    paging = panel_view.resolved_paging(loaded, Paging().viewing(tab="", open_panels=["p"]), focus_chart=None)

    frames = panel_data.panel_frames(store, loaded, paging, visible_open_panels(loaded.panels, "", paging))

    assert set(frames["p"].run_id) == {run.id for run in runs[-20:]}
