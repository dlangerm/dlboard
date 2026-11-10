"""A project page's grid of cards: which page and filter its URL asks for, and when its pager and filter show."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from dash import html

from dlboard.conftest import find_props
from dlboard.serve._pages._card_grid import PAGE_SIZE, CardGrid, CardQuery, Cards, CardSource, card_grid

if TYPE_CHECKING:
    from dlboard.models import DataStore

_GRID = CardGrid.EXPERIMENTS
_NO_STORE = cast("DataStore[...]", None)  # the stub source below reads none


def _source(names: list[str], empty: str | None = "none yet") -> CardSource:
    def read(_store: object, _project_id: int, name_contains: str, offset: int) -> Cards:
        matching = [n for n in names if name_contains in n]
        return Cards(len(matching), [html.Div(n) for n in matching[offset : offset + PAGE_SIZE]])

    return CardSource(
        grid=_GRID, label="Things", read=read, empty=lambda _p, _q: html.Div(empty) if empty else None
    )


def _shown(grid: Any) -> list[str]:  # noqa: ANN401
    cards = (find_props(grid, _GRID.list_id) or {})["children"]
    return [card.children for card in cards.children] if hasattr(cards, "children") else []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ({}, CardQuery()),
        ({"exps_q": " lr ", "exps_page": "3"}, CardQuery(q="lr", page=3)),
        ({"cmps_q": "lr", "cmps_page": "3"}, CardQuery()),  # another grid's
        ({"exps_page": "0"}, CardQuery()),
        ({"exps_page": "two"}, CardQuery()),
    ],
    ids=["none", "its-own", "another-grids", "page-zero", "not-a-number"],
)
def test_a_grid_reads_its_own_filter_and_page_from_the_url(
    query: dict[str, str], expected: CardQuery
) -> None:
    assert CardQuery.of(_GRID, query) == expected


def test_a_page_past_the_last_shows_the_last() -> None:
    names = [f"n{i:02}" for i in range(PAGE_SIZE + 5)]

    grid = card_grid(_source(names), _NO_STORE, 1, CardQuery(page=9))

    assert _shown(grid) == names[PAGE_SIZE:]
    assert (find_props(grid, _GRID.pager_id) or {})["value"] == 2


@pytest.mark.parametrize(
    ("count", "query", "controls"),
    [(3, CardQuery(), False), (PAGE_SIZE + 1, CardQuery(), True), (3, CardQuery(q="n"), True)],
    ids=["one-page", "more-than-a-page", "filtered"],
)
def test_the_filter_and_pager_show_only_once_there_is_more_than_a_page_or_a_filter(
    count: int, query: CardQuery, *, controls: bool
) -> None:
    grid = card_grid(_source([f"n{i}" for i in range(count)]), _NO_STORE, 1, query)

    shown: bool = (find_props(grid, _GRID.filter_id) or {})["style"]["display"] != "none"
    assert shown is controls
    assert ((find_props(grid, _GRID.pager_id) or {})["style"]["display"] != "none") is (count > PAGE_SIZE)


def test_a_grid_with_nothing_to_show_or_say_is_not_drawn() -> None:
    assert card_grid(_source([], empty=None), _NO_STORE, 1, CardQuery()) is None
    assert card_grid(_source([], empty="none yet"), _NO_STORE, 1, CardQuery()) is not None
