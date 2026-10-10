# pyright: reportPrivateUsage=false
"""Tests for which panels `accordion_view` renders: one page of the active tab's, after the name filter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from dlboard.conftest import find_props as _find_props
from dlboard.models._view import ChartInstance, PanelInstance
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve._pages._experiment import _experiment_page_state as state
from dlboard.serve._pages._experiment._paging import ChartPaging, PageSizes, Paging

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
    open_panels: list[str] | None = None,
    focus_chart: str | None = None,
) -> Any:  # noqa: ANN401
    page = store.get_or_create_page(state.BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(
        page.model_copy(
            update={
                "panels": panels,
                "page_settings": {state.OPEN_PANEL_KEY: open_panels or [], state.ACTIVE_TAB_KEY: active_tab},
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


def _charts(count: int) -> list[ChartInstance[Any, Any]]:
    return [
        ChartInstance[Any, Any](chart_type="line", parameters={"column": f"m{i}", "x_axis": "step"})
        for i in range(count)
    ]


def _chart_indexes_shown(container: Any, panel: str, count: int) -> list[int]:  # noqa: ANN401
    return [i for i in range(count) if _find_props(container.children, state.chart_content_id(panel, i))]


SIX_CHARTS = Paging(sizes=PageSizes(charts=6))


@pytest.mark.parametrize(
    ("page", "expected"), [(1, range(6)), (2, range(6, 12)), (3, range(12, 14)), (99, range(12, 14))]
)
def test_only_the_requested_page_of_a_panels_charts_is_rendered_under_their_own_indexes(
    store: SQLLiteStore, experiment_id: int, page: int, expected: range
) -> None:
    panels = [PanelInstance[Any, Any](name="p", charts=_charts(14))]
    paging = SIX_CHARTS.with_charts("p", ChartPaging(page=page))

    container = _render(store, experiment_id, panels, paging, open_panels=["p"])

    assert _chart_indexes_shown(container, "p", 14) == list(expected)


def test_a_panels_data_is_fetched_for_the_shown_charts_only(
    store: SQLLiteStore, experiment_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetched: list[frozenset[str] | None] = []
    fetch_metrics = store.fetch_metrics

    def spy(experiment_id: int, **kwargs: Any) -> Any:  # noqa: ANN401
        fetched.append(kwargs["keys"])
        return fetch_metrics(experiment_id, **kwargs)

    monkeypatch.setattr(store, "fetch_metrics", spy)
    panels = [PanelInstance[Any, Any](name="p", charts=_charts(14))]

    _render(store, experiment_id, panels, SIX_CHARTS.with_charts("p", ChartPaging(page=2)), open_panels=["p"])

    [keys] = fetched
    assert keys is not None
    assert {"m6", "m11"} <= keys
    assert not keys & {"m0", "m5", "m12"}


def test_the_chart_filter_matches_metric_names_before_paging(store: SQLLiteStore, experiment_id: int) -> None:
    panels = [PanelInstance[Any, Any](name="p", charts=_charts(14))]
    paging = SIX_CHARTS.with_charts("p", ChartPaging(q="m1"))  # m1, m10 .. m13

    container = _render(store, experiment_id, panels, paging, open_panels=["p"])

    assert _chart_indexes_shown(container, "p", 14) == [1, 10, 11, 12, 13]


def test_a_chart_link_lands_on_the_page_of_charts_holding_it(store: SQLLiteStore, experiment_id: int) -> None:
    charts = _charts(14)
    panels = [PanelInstance[Any, Any](name="p", charts=charts)]

    container = _render(store, experiment_id, panels, SIX_CHARTS, focus_chart=charts[9].id)

    assert _chart_indexes_shown(container, "p", 14) == list(range(6, 12))
    carried = _find_props(container.children, state.PANEL_PAGING_ID)
    assert carried is not None
    assert Paging.model_validate_json(carried["data"]).chart_paging("p").page == 2


def test_the_page_size_choices_are_bare_numbers_in_the_footer_with_the_pager(
    store: SQLLiteStore, experiment_id: int
) -> None:
    container = _render(store, experiment_id, _numbered(12), Paging())

    area = cast("Any", _find_props(container.children, state.PANEL_AREA_ID))
    select = _find_props(area["children"], state.PANEL_PAGE_SIZE_ID)
    assert select is not None
    assert [option["label"] for option in select["data"]] == ["5", "10", "20", "50"]
    assert _find_props(area["children"], state.PANEL_PAGER_ID) is not None


@pytest.mark.parametrize(
    ("panel_count", "charts_per_panel", "panel_select", "chart_select"),
    [(5, 6, False, False), (6, 6, True, False), (1, 7, False, True), (6, 7, True, True)],
)
def test_a_page_size_choice_is_only_offered_once_something_is_big_enough_to_page(  # noqa: PLR0913
    store: SQLLiteStore,
    experiment_id: int,
    panel_count: int,
    charts_per_panel: int,
    *,
    panel_select: bool,
    chart_select: bool,
) -> None:
    panels = [
        PanelInstance[Any, Any](name=f"panel {i:02}", charts=_charts(charts_per_panel))
        for i in range(panel_count)
    ]

    container = _render(store, experiment_id, panels, Paging())

    assert (_find_props(container.children, state.PANEL_PAGE_SIZE_ID) is not None) is panel_select
    assert (_find_props(container.children, state.CHART_PAGE_SIZE_ID) is not None) is chart_select


def _panel_header_filter(container: Any, panel: str) -> dict[str, Any] | None:  # noqa: ANN401
    return _find_props(container.children, state.chart_filter_id(panel))


def test_a_panel_with_enough_charts_has_its_chart_filter_in_its_header_and_its_pager_under_the_charts(
    store: SQLLiteStore, experiment_id: int
) -> None:
    panels = [PanelInstance[Any, Any](name="p", charts=_charts(14))]
    paging = SIX_CHARTS.with_charts("p", ChartPaging(q="m1"))

    container = _render(store, experiment_id, panels, paging, open_panels=["p"])

    header = _find_props(container.children, state.panel_header_id("p"))
    assert header is not None
    assert _find_props(header["children"], state.chart_filter_id("p")) is not None
    body = _find_props(container.children, state.panel_content_id("p"))
    assert body is not None
    assert _find_props(body["children"], state.chart_filter_id("p")) is None
    assert _find_props(container.children, state.chart_pager_slot_id("p")) is not None


@pytest.mark.parametrize(
    ("chart_count", "query", "offered"), [(6, "", False), (7, "", True), (3, "m1", True)]
)
def test_a_panels_chart_filter_is_only_offered_when_there_are_charts_to_page_or_one_is_on(
    store: SQLLiteStore, experiment_id: int, chart_count: int, query: str, *, offered: bool
) -> None:
    panels = [PanelInstance[Any, Any](name="p", charts=_charts(chart_count))]

    container = _render(store, experiment_id, panels, Paging().with_charts("p", ChartPaging(q=query)))

    assert (_panel_header_filter(container, "p") is not None) is offered
