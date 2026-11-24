"""Tests for the experiment page's pure paging and filtering logic."""

from __future__ import annotations

import json
from itertools import chain, pairwise
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

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


@given(
    st.text() | st.text(alphabet="aAµßİ ", max_size=4), st.data()
)  # letters whose case is not one letter's
def test_a_filter_matches_a_name_each_of_its_words_is_drawn_from_in_order(
    text: str, data: st.DataObject
) -> None:
    """`vl loss` matches `val/loss`, in any case; an empty filter matches everything."""
    kept = st.lists(st.booleans(), min_size=len(text), max_size=len(text))  # the characters a word keeps
    words = [
        "".join(char for char, keep in zip(text, mask, strict=True) if keep)
        for mask in data.draw(st.lists(kept, max_size=3))
    ]

    assert paging.fuzzy_match(" ".join(words).upper(), text)


@pytest.mark.parametrize(("query", "text"), [("ssol", "loss"), ("loss acc", "val/loss")])
def test_a_filter_matches_no_name_missing_a_word_or_its_order(query: str, text: str) -> None:
    assert not paging.fuzzy_match(query, text)


@given(st.lists(st.integers(), max_size=30), st.integers(), st.integers(min_value=1, max_value=10))
def test_a_page_is_its_slice_of_the_items_and_a_number_out_of_range_the_nearest_page(
    items: list[int], number: int, per_page: int
) -> None:
    page = paging.page_slice(items, number, per_page)
    pages = [paging.page_slice(items, at, per_page).items for at in range(1, page.total + 1)]

    assert page.number == sorted((1, number, page.total))[1]
    assert page.items == pages[page.number - 1]
    assert list(chain.from_iterable(pages)) == items
    assert all(len(full) == per_page for full in pages[:-1])
    assert pages[-1] or not items  # only nothing at all is one empty page


@given(
    st.lists(st.integers(min_value=1, max_value=8), max_size=20),
    st.integers(),
    st.integers(min_value=1, max_value=6),
)
def test_a_weighted_page_holds_what_fits_its_budget_and_one_too_heavy_for_any_holds_it_alone(
    weights: list[int], number: int, budget: int
) -> None:
    items = list(range(len(weights)))
    page = paging.weighted_page_slice(items, weights, number, budget)
    pages = [paging.weighted_page_slice(items, weights, at, budget).items for at in range(1, page.total + 1)]

    assert page.number == sorted((1, number, page.total))[1]
    assert page.items == pages[page.number - 1]
    assert list(chain.from_iterable(pages)) == items
    assert all(len(held) == 1 or sum(weights[i] for i in held) <= budget for held in pages)
    assert all(sum(weights[i] for i in held) + weights[after[0]] > budget for held, after in pairwise(pages))
    if all(weight == 1 for weight in weights):
        assert page == paging.page_slice(items, number, budget)


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


def _from_url(query: dict[str, str]) -> paging.Paging:
    return paging.Paging.from_request({name: value for name, value in query.items() if value}, None)


# Few characters and small numbers, so the edges come up: a space, what a URL or JSON quotes, page 1 and 2.
_NAMES = st.text(alphabet=' a&=/"é', max_size=4)
_PAGES = st.integers(min_value=1, max_value=3)


@given(
    st.builds(
        paging.Paging,
        panel_q=_NAMES,
        panel_page=_PAGES,
        charts=st.dictionaries(_NAMES, st.builds(paging.ChartPaging, q=_NAMES, page=_PAGES)),
        tab=st.none() | _NAMES.filter(bool),  # `""`, the ungrouped tab, is always the first: the same as none
        open_panels=st.none() | st.lists(_NAMES).map(tuple),
        focus=st.none() | _PAGES,
        hidden=st.frozensets(st.integers()),
    )
)
def test_a_link_shows_what_its_page_showed(shown: paging.Paging) -> None:
    assert _from_url(shown.query) == shown


_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=4),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=4), inner, max_size=3),
    max_leaves=8,
).map(json.dumps)


@given(st.dictionaries(st.sampled_from(sorted(paging.Paging().query)), st.text() | _JSON))
def test_any_link_shows_a_page_whose_own_link_shows_it_again(query: dict[str, str]) -> None:
    shown = _from_url(query)

    assert _from_url(shown.query) == shown


@pytest.mark.parametrize(
    "raw",
    [
        {"panels_page": "0"},
        {"panels_page": "abc"},
        {"charts": "not json"},
        {"charts": '{"p": {"page": -1}}'},
        {"open": "not json"},
        {"run": "0"},
        {"run": "abc"},
    ],
)
def test_malformed_query_shows_the_first_page(raw: dict[str, str]) -> None:
    assert paging.Paging.from_request(raw, None) == paging.Paging()


def test_a_bad_value_in_a_link_costs_only_itself() -> None:
    """Regression (#672): one bad value reset every other, so a comparison drew again the runs its viewer had hidden."""
    parsed = paging.Paging.from_request({"hidden": "[3, 7]", "panels_page": "abc", "run": "5"}, None)

    assert parsed == paging.Paging.from_request({"hidden": "[3, 7]", "run": "5"}, None)
    assert parsed.panel_page == 1


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
        ({"pager_value": 4}, 4, "", 10),
        ({}, 3, "", 10),
    ],
)
def test_next_panel_paging_resets_the_page_unless_a_pager_was_clicked(
    kwargs: dict[str, Any], expected_page: int, expected_q: str, expected_size: int
) -> None:
    current = paging.Paging().with_panels(page=3)
    controls: dict[str, Any] = {
        "q": "",
        "pager_value": 3,
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


def test_only_a_tab_or_open_panels_the_viewer_chose_goes_in_the_url() -> None:
    panels = [PanelInstance[Any, Any](name="a"), PanelInstance[Any, Any](name="b", tab="Images")]

    defaults = paging.Paging().viewing(tab="", open_panels=["a"]).normalized(panels)
    chosen = paging.Paging().viewing(tab="Images", open_panels=[]).normalized(panels)

    assert defaults == paging.Paging()
    assert (chosen.query["tab"], chosen.query["open"]) == ("Images", "[]")
    assert (chosen.shown_tab(panels), chosen.opened(panels)) == ("Images", [])


def test_a_focus_is_in_the_address_and_is_cleared_with_none() -> None:
    focused = paging.Paging.from_request({"run": "17"}, None)

    assert (focused.focus, focused.query["run"]) == (17, "17")
    assert focused.with_focus(None).focus is None
    assert focused.with_focus(None).query["run"] == ""
