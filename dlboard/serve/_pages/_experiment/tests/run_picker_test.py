"""A picker of runs lists a page of them, keeps what it picked, and finds the rest by search (#374)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dlboard import models
from dlboard.serve._pages._experiment._run_picker import RUN_PICKER_LIMIT, run_options

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


def test_a_picker_lists_the_newest_page_of_runs_keeps_its_picks_and_searches_the_rest(
    store: SQLLiteStore, experiment_id: int
) -> None:
    runs = [
        store.create_run(models.NewRun(experiment_id=experiment_id, name=f"run-{i:03d}"))
        for i in range(RUN_PICKER_LIMIT + 10)
    ]
    oldest = runs[0]

    listed = run_options(store, experiment_id)
    kept = run_options(store, experiment_id, keep=[str(oldest.id)])
    found = run_options(store, experiment_id, search="RUN-000")

    assert [option["label"] for option in listed] == [run.name for run in reversed(runs[-RUN_PICKER_LIMIT:])]
    assert kept[0] == {"value": str(oldest.id), "label": "run-000"}
    assert len(kept) == RUN_PICKER_LIMIT + 1
    assert found == [{"value": str(oldest.id), "label": "run-000"}]
