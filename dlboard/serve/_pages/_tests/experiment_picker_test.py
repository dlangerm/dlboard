"""A picker of experiments lists a page of them, keeps what it picked, and finds the rest by search (#590)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dlboard import models
from dlboard.serve._pages._experiment_picker import EXPERIMENT_PICKER_LIMIT, experiment_options, picked_values

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


def test_a_picker_lists_the_newest_page_of_experiments_keeps_its_pick_and_searches_the_rest(
    store: SQLLiteStore,
) -> None:
    project = store.create_project(models.NewProject(name="p", description=""))
    experiments = [
        store.create_experiment(models.NewExperiment(project_id=project.id, name=f"exp-{i:03d}"))
        for i in range(EXPERIMENT_PICKER_LIMIT + 10)
    ]
    oldest = experiments[0]
    elsewhere = store.create_experiment(
        models.NewExperiment(project_id=store.create_project(models.NewProject(name="o", description="")).id)
    )

    listed = experiment_options(store, project.id)
    kept = experiment_options(store, project.id, keep=[str(oldest.id), str(elsewhere.id)])
    found = experiment_options(store, project.id, search="EXP-000")

    assert [o["label"] for o in listed] == [e.name for e in reversed(experiments[-EXPERIMENT_PICKER_LIMIT:])]
    # The one picked keeps its label; one of another project is never offered.
    assert kept[0] == {"value": str(oldest.id), "label": "exp-000"}
    assert len(kept) == EXPERIMENT_PICKER_LIMIT + 1
    assert found == [{"value": str(oldest.id), "label": "exp-000"}]


def test_what_a_select_or_a_multiselect_picked_is_a_list() -> None:
    assert (picked_values(None), picked_values("3"), picked_values(["3", "4"])) == ([], ["3"], ["3", "4"])
