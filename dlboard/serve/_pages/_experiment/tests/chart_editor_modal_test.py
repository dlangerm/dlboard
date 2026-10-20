"""The add/edit-chart modal previews exactly what a panel would show."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum

from dlboard import models
from dlboard.conftest import props as _to_props
from dlboard.plugins.charts.bar_chart import BarChart
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment._chart_editor_modal import render_chart_preview
from dlboard.serve._pages._experiment._page_model import DocumentSettings

if TYPE_CHECKING:
    from collections.abc import Callable

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

_TS = pendulum.datetime(2026, 1, 1, tz="UTC")

BarChart.register(allow_override=True)
LineChart.register(allow_override=True)
TableChart.register(allow_override=True)


def _log_a_run(store: SQLLiteStore, experiment_id: int) -> models.Run:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"loss": 0.5, "acc": 0.9},
                step=0,
                experiment_id=experiment_id,
                run_id=run.id,
                timestamp_utc=_TS,
            )
        ]
    )
    store.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment_id, {"lr": 0.1}))
    return run


def test_render_chart_preview_leaves_out_excluded_runs_like_the_real_panel(
    store: SQLLiteStore, experiment_id: int, sign_in_as: Callable[[str], None]
) -> None:
    """It used to render from the whole experiment, so it showed runs the panel itself hides."""
    sign_in_as("alice")
    kept, excluded = _log_a_run(store, experiment_id), _log_a_run(store, experiment_id)
    # Excluding a run branches into a view of alice's own (see `save_page`) -- preview from that
    # same view, not the (untouched) shared page, to see the exclusion it just made.
    view = repo.save_document_settings(
        store, repo.PageRef(experiment_id, None), DocumentSettings(excluded_runs=[excluded.id])
    )

    field_ids = [{"type": "chart-param", "field": "column"}, {"type": "chart-param", "field": "x_axis"}]
    preview, error = render_chart_preview(
        "line",
        ["loss", "step"],
        [None, None],
        field_ids,
        [],
        [],
        store=store,
        ref=repo.PageRef(experiment_id, view.id),
    )

    assert error == ""
    _title, plot = _to_props(preview)["children"]
    assert [s["name"] for s in _to_props(plot)["series"]] == [str(kept.id)]
