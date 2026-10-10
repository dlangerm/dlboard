# pyright: reportPrivateUsage=false
"""Tests for which panels `accordion_view` renders: one page of the active tab's, after the name filter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from dlboard.conftest import find_props as _find_props
from dlboard.models._view import ChartInstance, PanelInstance
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve._pages._experiment import _experiment_page_state as state
from dlboard.serve._pages._experiment._paging import PageSizes, Paging

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

LineChart.register(allow_override=True)

FIVE_PER_PAGE = Paging(sizes=PageSizes(panels=5))


def _render(  # noqa: PLR0913
    store: SQLLiteStore,
    experiment_id: int,
    panels: list[PanelInstance[Any, Any]],
    paging: Paging,
    *,
    active_tab: str = "",
    focus_chart: str | None = None,
) -> Any:  # noqa: ANN401
    page = store.get_or_create_page(state.BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(
        page.model_copy(
            update={
                "panels": panels,
                "page_settings": {state.OPEN_PANEL_KEY: [], state.ACTIVE_TAB_KEY: active_tab},
            }
        )
    )
    loaded = state.load_page(store, state.PageRef(experiment_id, None))
    return state.accordion_view(store, loaded, paging, focus_chart=focus_chart)


def _shown(container: Any, tab: str = "") -> list[str]:  # noqa: ANN401
    """Names of the panels in `tab`'s rendered accordion, in order."""
    accordion = _find_props(container.children, state.panel_accordion_id(tab))
    assert accordion is not None
    return [item.value for item in accordion["children"]]


def _numbered(count: int, tab: str = "") -> list[PanelInstance[Any, Any]]:
    return [PanelInstance[Any, Any](name=f"panel {i:02}", tab=tab) for i in range(count)]


@pytest.mark.parametrize(("page", "first", "last"), [(1, 0, 4), (2, 5, 9), (3, 10, 11), (99, 10, 11)])
def test_only_the_requested_page_of_panels_is_rendered(
    store: SQLLiteStore, experiment_id: int, page: int, first: int, last: int
) -> None:
    container = _render(store, experiment_id, _numbered(12), FIVE_PER_PAGE.with_panels(page=page))

    assert _shown(container) == [f"panel {i:02}" for i in range(first, last + 1)]


def test_the_name_filter_is_applied_before_paging(store: SQLLiteStore, experiment_id: int) -> None:
    container = _render(store, experiment_id, _numbered(30), FIVE_PER_PAGE.with_panels(q="2 1"))

    assert _shown(container) == ["panel 12", "panel 21"]


def test_only_the_active_tabs_panels_are_rendered(store: SQLLiteStore, experiment_id: int) -> None:
    panels = [*_numbered(2), *_numbered(2, tab="Images")]

    container = _render(store, experiment_id, panels, Paging(), active_tab="Images")

    assert _shown(container, "Images") == ["panel 00", "panel 01"]
    assert _find_props(container.children, state.panel_accordion_id("")) is None


def test_the_emitted_paging_is_the_one_rendered(store: SQLLiteStore, experiment_id: int) -> None:
    container = _render(store, experiment_id, _numbered(12), FIVE_PER_PAGE.with_panels(page=99))

    carried = _find_props(container.children, state.PANEL_PAGING_ID)
    assert carried is not None
    assert Paging.model_validate_json(carried["data"]).panel_page == 3


def test_a_chart_link_lands_on_the_page_of_panels_holding_it(store: SQLLiteStore, experiment_id: int) -> None:
    chart = ChartInstance[Any, Any](chart_type="line", parameters={"column": "loss", "x_axis": "step"})
    panels = [
        PanelInstance[Any, Any](name=f"panel {i:02}", charts=[chart] if i == 7 else []) for i in range(12)
    ]

    container = _render(store, experiment_id, panels, FIVE_PER_PAGE, focus_chart=chart.id)

    assert "panel 07" in _shown(container)
    accordion = cast("Any", _find_props(container.children, state.panel_accordion_id("")))
    assert accordion["value"] == ["panel 07"]
