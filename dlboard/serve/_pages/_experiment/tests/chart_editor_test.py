"""The chart editor names what it edits by id, previews a new chart as a panel would show it, and draws an edit in its card."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pendulum
from dash import no_update
from structlog.testing import capture_logs

from dlboard import models
from dlboard.conftest import props as _to_props
from dlboard.plugins.charts.bar_chart import BarChart
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment._chart_editor import (
    render_card_drafts,
    render_chart_preview,
)
from dlboard.serve._pages._experiment._page_model import BasicExperimentPage, ChartLocation, DocumentSettings

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


def test_render_chart_preview_draws_the_selected_runs_like_the_real_panel(
    store: SQLLiteStore, experiment_id: int, sign_in_as: Callable[[str], None]
) -> None:
    """It used to render from the whole experiment, so it showed runs the panel itself leaves out."""
    sign_in_as("alice")
    kept, _unselected = _log_a_run(store, experiment_id), _log_a_run(store, experiment_id)
    # Selecting runs branches into a view of alice's own (see `save_page`) -- preview from that
    # same view, not the (untouched) shared page, to see the selection it just made.
    view = repo.save_document_settings(
        store, repo.PageRef(experiment_id, None), DocumentSettings(selected_runs=[kept.id])
    )

    form = _form(
        experiment_id=experiment_id,
        page_json=repo.load_page(store, repo.PageRef(experiment_id, view.id)).model_dump_json(),
    )
    preview, error = render_chart_preview(cast("Any", form), store=store)

    assert error == ""
    _title, plot = _to_props(preview)["children"]
    assert [s["name"] for s in _to_props(plot)["series"]] == [str(kept.id)]


def _form(**changes: Any) -> dict[str, Any]:  # noqa: ANN401 -- the values of a form read by a callback
    form: dict[str, Any] = {
        "chart_type_name": "line",
        "values": ["loss", "step"],
        "checked_values": [None, None],
        "field_ids": [{"type": "chart-param", "field": "column"}, {"type": "chart-param", "field": "x_axis"}],
        "table_ids": [],
        "table_values": [],
        "experiment_id": 0,
        "page_json": "",
        "paging_json": "",
        "target": None,
    }
    return form | changes


def _page_with_a_chart(store: SQLLiteStore, experiment_id: int) -> tuple[models.PanelInstance[Any, Any], str]:
    panel = models.PanelInstance[Any, Any](
        name="train",
        charts=[models.ChartInstance(chart_type="line", parameters={"column": "acc", "x_axis": "step"})],
    )
    page = BasicExperimentPage(id=0, experiment_id=experiment_id, owner_id=None, name="", panels=[panel])
    _log_a_run(store, experiment_id)
    return panel, page.model_dump_json()


def test_an_edited_chart_is_drawn_in_its_own_card_under_ids_of_its_own_and_the_other_cards_stay_empty(
    store: SQLLiteStore, experiment_id: int
) -> None:
    panel, page_json = _page_with_a_chart(store, experiment_id)
    location = ChartLocation(panel, 0, panel.charts[0])
    form = _form(experiment_id=experiment_id, page_json=page_json)
    draft_ids = [
        {"type": "chart-draft", "panel": "train", "index": 0},
        {"type": "chart-draft", "panel": "val", "index": 0},
    ]

    drafts, error = render_card_drafts(
        cast("Any", form),
        location,
        models.DEFAULT_RENDER_CONTEXT.page_size,
        cast("Any", draft_ids),
        store=store,
    )

    assert error == ""
    assert drafts[1] is None
    _title, plot = _to_props(drafts[0])["children"]
    assert _to_props(plot)["data"][0]["1"] == 0.5  # the form's loss, not the saved chart's acc (0.9)


def test_a_form_that_makes_no_valid_chart_leaves_the_draft_as_it_was_and_says_why(
    store: SQLLiteStore, experiment_id: int
) -> None:
    panel, page_json = _page_with_a_chart(store, experiment_id)
    location = ChartLocation(panel, 0, panel.charts[0])
    form = _form(
        experiment_id=experiment_id, page_json=page_json, values=[None, "step"]
    )  # the column is required
    draft_ids = [{"type": "chart-draft", "panel": "train", "index": 0}]

    with capture_logs() as logs:
        drafts, error = render_card_drafts(
            cast("Any", form),
            location,
            models.DEFAULT_RENDER_CONTEXT.page_size,
            cast("Any", draft_ids),
            store=store,
        )

    assert drafts == [no_update]
    assert "Fill in required fields" in error
    # An incomplete form is expected while someone types: a debug line, not a traceback on every keystroke.
    assert [(entry["log_level"], "exc_info" in entry) for entry in logs] == [("debug", False)]
