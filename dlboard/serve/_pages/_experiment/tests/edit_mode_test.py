"""Tests for which edit controls `accordion_view` offers for a page's current state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from dlboard.conftest import find_props as _find_props
from dlboard.models._view import PanelInstance
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve._pages._experiment import _experiment_page_state as state
from dlboard.serve._pages._experiment._paging import Paging

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

LineChart.register(allow_override=True)


# ---- toolbar: auto-generate charts (empty view) vs suggest charts (non-empty view) ----


@pytest.mark.parametrize(
    ("panels", "auto_populate_offered"),
    [
        pytest.param([], True, id="empty-view-offers-auto-populate"),
        pytest.param(
            [PanelInstance[Any, Any](name="p")], False, id="a-view-with-panels-offers-suggest-charts"
        ),
    ],
)
def test_accordion_view_offers_auto_populate_only_for_an_empty_view(
    store: SQLLiteStore,
    experiment_id: int,
    panels: list[PanelInstance[Any, Any]],
    *,
    auto_populate_offered: bool,
) -> None:
    page = store.get_or_create_page(state.BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(page.model_copy(update={"panels": panels}))

    container = state.accordion_view(
        store, state.load_page(store, state.PageRef(experiment_id, None)), Paging()
    )

    children = cast("list[Any]", container.children)
    assert (_find_props(children, state.AUTO_POPULATE_OPEN_ID) is not None) is auto_populate_offered
    assert (_find_props(children, state.SUGGEST_CHARTS_BUTTON_ID) is not None) is not auto_populate_offered


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
        state.DELETE_PANEL_BUTTON.of("p"),
    )
    assert delete_button is not None
    assert delete_button.get("disabled") is not True
