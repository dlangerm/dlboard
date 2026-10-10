"""Tests for the experiment page's pure paging and filtering logic."""

from __future__ import annotations

from typing import Any

import pytest

from dlboard.models._view import ChartInstance, PanelInstance
from dlboard.plugins.charts.image_series import ImageChart
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve._pages._experiment import _paging as paging

LineChart.register(allow_override=True)
ImageChart.register(allow_override=True)


def _line(column: str) -> ChartInstance[Any, Any]:
    return ChartInstance[Any, Any](chart_type="line", parameters={"column": column, "x_axis": "step"})


def _panels(count: int, tab: str = "") -> list[PanelInstance[Any, Any]]:
    return [PanelInstance[Any, Any](name=f"panel {i}", tab=tab) for i in range(count)]


@pytest.mark.parametrize(
    ("query", "text", "expected"),
    [
        ("", "anything", True),
        ("vl", "val/loss", True),
        ("LOSS", "train_loss", True),
        ("vl loss", "val/loss", True),
        ("ssol", "loss", False),
        ("loss acc", "val/loss", False),
    ],
)
def test_fuzzy_match(query: str, text: str, *, expected: bool) -> None:
    assert paging.fuzzy_match(query, text) is expected


@pytest.mark.parametrize(
    ("number", "expected_items", "expected_number"),
    [(1, [0, 1, 2], 1), (2, [3, 4, 5], 2), (3, [6], 3), (0, [0, 1, 2], 1), (99, [6], 3)],
)
def test_page_slice_clamps_into_range(number: int, expected_items: list[int], expected_number: int) -> None:
    page = paging.page_slice(list(range(7)), number, 3)
    assert (page.items, page.number, page.total) == (expected_items, expected_number, 3)


def test_empty_sequence_is_one_empty_page() -> None:
    page = paging.page_slice(list[int](), 5, 3)
    assert (page.items, page.number, page.total) == ([], 1, 1)


def test_visible_panels_filters_the_tab_before_slicing() -> None:
    panels = [*_panels(12), PanelInstance[Any, Any](name="other", tab="t")]
    sizes = paging.PageSizes(panels=5)

    first = paging.visible_panels(panels, "", paging.Paging(sizes=sizes))
    assert [p.name for p in first.items] == [f"panel {i}" for i in range(5)]
    assert first.total == 3

    filtered = paging.visible_panels(panels, "", paging.Paging(panels_q="l 11", sizes=sizes))
    assert [p.name for p in filtered.items] == ["panel 11"]


def test_visible_chart_indexes_keep_absolute_positions() -> None:
    panel = PanelInstance[Any, Any](name="p", charts=[_line(f"m{i}") for i in range(30)])
    current = paging.Paging(sizes=paging.PageSizes(charts=6)).with_charts(
        "p", paging.ChartPaging(q="m2", page=1)
    )

    shown = paging.visible_chart_indexes(panel, current)
    assert shown.items == [2, 12, 20, 21, 22, 23]
    assert shown.total == 2  # m2, m12, m20..m29
    second = paging.visible_chart_indexes(panel, current.with_charts("p", paging.ChartPaging(q="m2", page=2)))
    assert second.items == [24, 25, 26, 27, 28, 29]


def test_chart_search_keys_cover_metrics_and_artifacts_but_not_axes() -> None:
    assert paging.chart_search_keys(_line("val/loss")) == {"line", "val/loss"}
    image = ChartInstance[Any, Any](chart_type="image", parameters={"key": "preview"})
    assert "preview" in paging.chart_search_keys(image)


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"panels_q": "loss", "panels_page": "3"},
        {"charts": '{"p": {"q": "acc", "page": 2}}'},
        {"panels_page": "2", "charts": '{"p": {"page": 4}}', "panels_q": "x"},
    ],
)
def test_query_round_trips(raw: dict[str, str]) -> None:
    parsed = paging.Paging.from_request(raw, None)
    url = {name: value for name, value in parsed.query.items() if value}
    assert paging.Paging.from_request(url, None) == parsed


@pytest.mark.parametrize(
    "raw",
    [{"panels_page": "0"}, {"panels_page": "abc"}, {"charts": "not json"}, {"charts": '{"p": {"page": -1}}'}],
)
def test_malformed_query_shows_the_first_page(raw: dict[str, str]) -> None:
    assert paging.Paging.from_request(raw, None) == paging.Paging()


@pytest.mark.parametrize(
    ("cookie", "expected"),
    [
        (None, paging.PageSizes()),
        ('{"panels": 20, "charts": 24}', paging.PageSizes(panels=20, charts=24)),
        ("not json", paging.PageSizes()),
        ('{"panels": 7}', paging.PageSizes()),
    ],
)
def test_sizes_come_from_the_cookie_and_fall_back_to_defaults(
    cookie: str | None, expected: paging.PageSizes
) -> None:
    assert paging.Paging.from_request({"panels_page": "2"}, cookie).sizes == expected


def test_default_paging_adds_nothing_to_the_url() -> None:
    assert not any(paging.Paging().query.values())


def test_reveal_pages_to_the_panel_and_chart_clearing_filters_that_hide_them() -> None:
    charts = [_line(f"metric{i}") for i in range(20)]
    panels = [*_panels(12), PanelInstance[Any, Any](name="target", charts=charts)]
    hiding = (
        paging.Paging(panels_q="panel 1", sizes=paging.PageSizes(panels=5, charts=6))
        .with_panels(page=2)
        .with_charts("target", paging.ChartPaging(q="zzz"))
    )

    revealed = paging.reveal(panels, hiding, panel_name="target", chart_id=charts[13].id)

    assert revealed.panel_q == ""
    assert revealed.panel_page == 3
    assert revealed.chart_paging("target") == paging.ChartPaging(q="", page=3)


def test_reveal_of_an_unknown_target_leaves_paging_alone() -> None:
    current = paging.Paging(panels_q="x")
    assert paging.reveal(_panels(3), current, panel_name="missing") == current


@pytest.mark.parametrize(
    ("kwargs", "expected_page", "expected_q", "expected_size"),
    [
        ({"q": "loss"}, 1, "loss", 10),
        ({"size": 20}, 1, "", 20),
        ({"tab_changed": True}, 1, "", 10),
        ({"pager_values": [4, 4]}, 4, "", 10),
        ({"pager_values": [3, 2]}, 2, "", 10),
        ({}, 3, "", 10),
    ],
)
def test_next_panel_paging_resets_the_page_unless_a_pager_was_clicked(
    kwargs: dict[str, Any], expected_page: int, expected_q: str, expected_size: int
) -> None:
    current = paging.Paging().with_panels(page=3)
    controls: dict[str, Any] = {
        "q": "",
        "pager_values": [3, 3],
        "size": 10,
        "chart_size": 12,
        "tab_changed": False,
    } | kwargs

    result = paging.next_panel_paging(current, **controls)

    assert (result.panel_page, result.panel_q, result.sizes.panels) == (
        expected_page,
        expected_q,
        expected_size,
    )


@pytest.mark.parametrize(
    ("filters", "pagers", "expected"),
    [
        ({"a": "loss"}, {}, {"a": paging.ChartPaging(q="loss")}),
        ({"a": "loss"}, {"a": 4}, {"a": paging.ChartPaging(q="loss")}),
        ({}, {"a": 4}, {"a": paging.ChartPaging(page=4)}),
        ({"a": "", "b": ""}, {"a": 1}, {}),
        ({}, {"a": 2, "b": 3}, {"a": paging.ChartPaging(page=2), "b": paging.ChartPaging(page=3)}),
    ],
)
def test_next_chart_paging_resets_a_panels_page_when_its_filter_changes(
    filters: dict[str, str], pagers: dict[str, int], expected: dict[str, paging.ChartPaging]
) -> None:
    assert paging.next_chart_paging(paging.Paging(), filters=filters, pagers=pagers).charts == expected


def test_next_chart_paging_only_changes_the_panels_that_differ() -> None:
    current = paging.Paging().with_charts("a", paging.ChartPaging(q="x", page=2))

    assert paging.next_chart_paging(current, filters={"a": "x"}, pagers={"a": 2}) == current


def test_a_new_charts_per_page_size_keeps_every_filter_but_returns_to_the_first_page() -> None:
    current = paging.Paging().with_charts("a", paging.ChartPaging(q="x", page=3))

    resized = current.with_sizes(charts=24)

    assert resized.sizes.charts == 24
    assert resized.chart_paging("a") == paging.ChartPaging(q="x", page=1)


def test_a_panel_at_its_defaults_is_the_same_as_one_never_mentioned() -> None:
    assert paging.Paging().with_charts("a", paging.ChartPaging()) == paging.Paging()
