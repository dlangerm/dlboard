"""The chart editor names what it edits by id, previews a new chart as a panel would show it, and draws an edit in its card."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pendulum
import pytest
from dash import no_update
from pydantic import ValidationError

from dlboard import models
from dlboard.conftest import props as _to_props
from dlboard.plugins.charts.bar_chart import BarChart
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment._chart_editor import (
    _TARGET,
    AddChart,
    ChartRef,
    EditCharts,
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


@pytest.mark.parametrize(
    "target",
    [
        pytest.param(EditCharts(charts=(ChartRef(chart_id="abc"),)), id="edit"),
        pytest.param(AddChart(panel="train"), id="add"),
    ],
)
def test_what_the_editor_is_open_on_survives_being_kept_in_the_page(target: EditCharts | AddChart) -> None:
    assert _TARGET.validate_python(target.model_dump()) == target


def test_an_editing_session_takes_at_least_one_chart_and_no_more_than_it_can_edit() -> None:
    with pytest.raises(ValidationError):
        EditCharts(charts=())
    with pytest.raises(ValidationError):
        EditCharts(charts=(ChartRef(chart_id="a"), ChartRef(chart_id="b")))


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
    assert [s["name"] for s in _to_props(plot)["series"]] == [
        "1"
    ]  # the form's `loss`, not the saved chart's `acc`


def test_a_form_that_makes_no_valid_chart_leaves_the_draft_as_it_was_and_says_why(
    store: SQLLiteStore, experiment_id: int
) -> None:
    panel, page_json = _page_with_a_chart(store, experiment_id)
    location = ChartLocation(panel, 0, panel.charts[0])
    form = _form(
        experiment_id=experiment_id, page_json=page_json, values=[None, "step"]
    )  # the column is required
    draft_ids = [{"type": "chart-draft", "panel": "train", "index": 0}]

    drafts, error = render_card_drafts(
        cast("Any", form),
        location,
        models.DEFAULT_RENDER_CONTEXT.page_size,
        cast("Any", draft_ids),
        store=store,
    )

    assert drafts == [no_update]
    assert "Fill in required fields" in error
