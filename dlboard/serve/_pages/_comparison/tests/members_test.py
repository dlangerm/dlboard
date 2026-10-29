"""The comparison's list of runs: a row for each member, whichever experiment it is of, and when the list is drawn again."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pendulum
import pytest

from dlboard import models
from dlboard.serve._pages._comparison import _members as members
from dlboard.serve._pages._experiment._page_model import BasicExperimentPage, DocumentSettings

_TS = pendulum.datetime(2026, 1, 1, tz="UTC")


if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


@pytest.fixture(autouse=True)
def _plain_run_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """Links to a run are plain paths, with no running Dash app for `relative_path` to read the prefix of."""
    monkeypatch.setattr("dlboard.serve._pages._experiment._focus.relative_path", str)


def _comparison_of_two_experiments(store: SQLLiteStore) -> tuple[BasicExperimentPage, list[models.Run]]:
    """A comparison of a run called "fluffy" from each of two experiments, which logged a different learning rate."""
    project = store.create_project(models.NewProject(name="p", description=""))
    runs: list[models.Run] = []
    for lr, name in ((0.1, "baseline"), (0.3, "sweep")):
        experiment = store.create_experiment(models.NewExperiment(project_id=project.id, name=name))
        run = store.create_run(models.NewRun(experiment_id=experiment.id, name="fluffy"))
        store.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment.id, {"lr": lr}))
        runs.append(run)
    owner = store.get_or_create_user(models.Principal.unverified("owner"))
    created = store.create_comparison(
        BasicExperimentPage,
        models.NewPage[Any, Any](project_id=project.id, owner_id=owner.id, name="c"),
        frozenset(run.id for run in runs),
    )
    page = cast("BasicExperimentPage", store.get_view(BasicExperimentPage, created.id))
    return page.with_settings(DocumentSettings(hparam_columns=["lr"])), runs


def test_a_row_for_each_member_says_whose_it_is_whether_it_is_drawn_and_its_chosen_hyperparameters(
    store: SQLLiteStore,
) -> None:
    page, (baseline, sweep) = _comparison_of_two_experiments(store)

    rows = members._rows(store, store.comparison_runs(page.id), page, frozenset({sweep.id}), {})

    by_experiment = {row["_experiment"]: row for row in rows}
    assert set(by_experiment) == {"baseline", "sweep"}
    assert {name: (row["run_name"], row["_drawn"], row["lr"]) for name, row in by_experiment.items()} == {
        "baseline": ("fluffy", True, 0.1),
        "sweep": ("fluffy", False, 0.3),
    }
    assert by_experiment["baseline"]["_href"] == f"/experiment/{baseline.experiment_id}?run={baseline.id}"


def test_the_list_is_drawn_again_when_what_is_hidden_changes_and_not_when_nothing_did(
    store: SQLLiteStore,
) -> None:
    page, (_, sweep) = _comparison_of_two_experiments(store)
    runs = store.comparison_runs(page.id)

    def signature(hidden: frozenset[int]) -> str:
        return members._signature(members._rows(store, runs, page, hidden, {}), can_edit=True)

    assert signature(frozenset()) == signature(frozenset())
    assert signature(frozenset()) != signature(frozenset({sweep.id}))
    assert members._signature([], can_edit=True) != members._signature([], can_edit=False)


def test_a_metric_column_holds_the_latest_value_of_each_members_run(store: SQLLiteStore) -> None:
    """A column named for a metric the members logged shows that run's latest value, where a hyperparameter column would be blank."""
    page, (baseline, sweep) = _comparison_of_two_experiments(store)
    page = page.with_settings(DocumentSettings(hparam_columns=["lr", "val/acc"]))
    for run, acc in ((baseline, 0.5), (sweep, 0.9)):
        store.log_metrics(
            [
                models.LoggedMetrics(
                    metrics={"val/acc": acc},
                    step=1,
                    experiment_id=run.experiment_id,
                    run_id=run.id,
                    timestamp_utc=_TS,
                )
            ]
        )

    rows = members._rows(store, store.comparison_runs(page.id), page, frozenset(), {})

    assert {row["_experiment"]: row["val/acc"] for row in rows} == {"baseline": 0.5, "sweep": 0.9}
