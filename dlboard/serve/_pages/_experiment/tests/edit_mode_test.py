"""Tests for which edit controls `accordion_view` offers for a page's current state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from dlboard.conftest import find_props as _find_props
from dlboard.models._view import PanelInstance
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve._pages._experiment import _experiment_page_state as state
from dlboard.serve._pages._experiment._paging import Paging

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

LineChart.register(allow_override=True)


# ---- toolbar: auto-generate charts (empty view) vs suggest charts (non-empty view) ----


def test_accordion_view_shows_auto_populate_when_view_is_empty(
    store: SQLLiteStore, experiment_id: int
) -> None:
    container = state.accordion_view(
        store, state.load_page(store, state.PageRef(experiment_id, None)), Paging()
    )

    assert (
        # pyrefly: ignore [unknown-argument-type]
        _find_props(cast("Any", container).children, state.AUTO_POPULATE_BUTTON_ID) is not None
    )
    assert (
        # pyrefly: ignore [unknown-argument-type]
        _find_props(cast("Any", container).children, state.SUGGEST_CHARTS_BUTTON_ID) is None
    )


def test_accordion_view_shows_suggest_charts_when_view_is_not_empty(
    store: SQLLiteStore, experiment_id: int
) -> None:
    page = store.get_or_create_page(state.BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(page.model_copy(update={"panels": [PanelInstance[Any, Any](name="p")]}))

    container = state.accordion_view(
        store, state.load_page(store, state.PageRef(experiment_id, None)), Paging()
    )

    assert (
        # pyrefly: ignore [unknown-argument-type]
        _find_props(cast("Any", container).children, state.SUGGEST_CHARTS_BUTTON_ID) is not None
    )
    assert (
        # pyrefly: ignore [unknown-argument-type]
        _find_props(cast("Any", container).children, state.AUTO_POPULATE_BUTTON_ID) is None
    )


def test_accordion_view_delete_panel_reachable_without_opening_a_panel(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """The whole point: delete/rename/reorder must be usable without opening (fetching) the panel."""
    page = store.get_or_create_page(state.BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(page.model_copy(update={"panels": [PanelInstance[Any, Any](name="p")]}))

    container = state.accordion_view(
        store, state.load_page(store, state.PageRef(experiment_id, None)), Paging()
    )

    delete_button = _find_props(
        # pyrefly: ignore [unknown-argument-type]
        cast("Any", container).children,
        state.delete_panel_button_id("p"),
    )
    assert delete_button is not None
    assert delete_button.get("disabled") is not True
