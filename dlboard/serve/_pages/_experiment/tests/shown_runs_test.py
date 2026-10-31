"""Which runs a page draws: the ones a view selected, else only the newest few, whatever an experiment holds."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

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
from dlboard.serve._pages._experiment._shown_runs import page_experiments, page_scope, shown_run_ids

if TYPE_CHECKING:
    import pytest

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

LineChart.register(allow_override=True)

_TS = pendulum.datetime(2026, 1, 1, tz="UTC")


def _runs(store: SQLLiteStore, experiment_id: int, count: int) -> list[models.Run]:
    """`count` runs that each logged a loss, oldest first."""
    # Distinct, ascending creation times: "newest" must not depend on how fast the runs were created (a flaky tie).
    runs = [
        store.create_run(models.NewRun(experiment_id=experiment_id, created_at=_TS.add(seconds=index)))
        for index in range(count)
    ]
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
    page = repo.load_page(store, repo.ExperimentRef(experiment_id, None))
    store.update_page(page.model_copy(update={"panels": [panel]}))
    loaded = repo.load_page(store, repo.ExperimentRef(experiment_id, None))
    paging = panel_view.resolved_paging(loaded, Paging().viewing(tab="", open_panels=["p"]), focus_chart=None)

    frames = panel_data.panel_frames(store, loaded, paging, visible_open_panels(loaded.panels, "", paging))

    assert set(frames["p"].for_chart(panel.charts[0]).run_id) == {run.id for run in runs[-20:]}


def _comparison(store: SQLLiteStore, project_id: int, run_ids: set[int]) -> page_model.BasicExperimentPage:
    comparison = store.create_comparison(
        page_model.BasicExperimentPage,
        models.NewPage[Any, Any](
            project_id=project_id,
            owner_id=store.get_or_create_user(models.Principal.unverified("alice")).id,
            name="sweep",
        ),
        frozenset(run_ids),
    )
    return cast("page_model.BasicExperimentPage", comparison)


def test_an_experiment_page_draws_its_own_experiments_runs_and_a_comparison_the_members_it_has_not_hidden(
    store: SQLLiteStore, experiment_id: int
) -> None:
    experiment = store.get_experiment(experiment_id)
    assert experiment is not None
    other = store.create_experiment(models.NewExperiment(project_id=experiment.project_id)).id
    first, second = _runs(store, experiment_id, 2), _runs(store, other, 1)
    shared = cast(
        "page_model.BasicExperimentPage",
        store.get_or_create_page(page_model.BasicExperimentPage, experiment_id=experiment_id),
    )
    members = {run.id for run in (*first, *second)}

    comparison = _comparison(store, experiment.project_id, members)

    assert page_scope(store, shared) == {experiment_id: frozenset(run.id for run in first)}
    assert page_scope(store, comparison, frozenset({first[0].id})) == {
        experiment_id: frozenset({first[1].id}),
        other: frozenset({second[0].id}),
    }
    assert page_experiments(store, shared) == [experiment_id]
    assert sorted(page_experiments(store, comparison)) == sorted([experiment_id, other])


def test_a_comparison_whose_members_are_all_hidden_draws_nothing_and_an_experiment_page_still_has_its_experiment(
    store: SQLLiteStore, experiment_id: int
) -> None:
    experiment = store.get_experiment(experiment_id)
    assert experiment is not None
    (run,) = _runs(store, experiment_id, 1)
    comparison = _comparison(store, experiment.project_id, {run.id})
    nothing_selected = cast(
        "page_model.BasicExperimentPage",
        store.get_or_create_page(page_model.BasicExperimentPage, experiment_id=experiment_id),
    ).with_settings(DocumentSettings(selected_runs=[]))

    assert page_scope(store, comparison, frozenset({run.id})) == {}
    assert page_scope(store, nothing_selected) == {experiment_id: frozenset()}


def test_a_focus_draws_just_that_run_but_only_if_it_is_a_live_run_of_the_experiment(
    store: SQLLiteStore, experiment_id: int, admin: models.User
) -> None:
    runs = _runs(store, experiment_id, 3)
    project_id = store.get_experiment(experiment_id).project_id  # pyrefly: ignore [missing-attribute]
    elsewhere = store.create_run(
        models.NewRun(experiment_id=store.create_experiment(models.NewExperiment(project_id=project_id)).id)
    )
    store.delete_run(runs[0].id, admin)
    page = repo.load_page(store, repo.ExperimentRef(experiment_id, None))
    own = frozenset({runs[1].id, runs[2].id})

    def drawn(focus: int | None) -> frozenset[int]:
        return shown_run_ids(store, experiment_id, page.document, focus)

    assert drawn(runs[1].id) == {runs[1].id}
    assert page_scope(store, page, focus=runs[2].id) == {experiment_id: {runs[2].id}}
    # A link to a run that is another experiment's, deleted, or not there at all draws what the view does.
    assert [drawn(None), drawn(elsewhere.id), drawn(runs[0].id), drawn(999_999)] == [own] * 4
