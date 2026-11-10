"""
A project page's cards a page at a time, with a filter by name: its experiments, and its comparisons (#502).

A project with hundreds of either draws a page of them, not every one, and says how many there are. Which page and what
filter are in the page URL's query (`?exps_q=`, `?exps_page=`, `?cmps_q=`, `?cmps_page=`), so a link shows what was seen.
The filter and the pager are always in the page, hidden while there is only one page and no filter: a callback that writes
to them must find them there.
"""

from __future__ import annotations

import math
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import dash_mantine_components as dmc
from dash import Input, Output, State, ctx
from pydantic import BaseModel, Field, ValidationError

from dlboard._compat import StrEnum
from dlboard.serve import ClientsideScript, Icon, get_data_store, icon
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import DivId, ValueId
from dlboard.serve._pages._dash_helpers import pager_props, section_label

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from dash import Dash
    from dash.development.base_component import Component

    from dlboard.models import DataStore

PAGE_SIZE: typing.Final = 24
"""Cards a page: a whole number of rows in one, two or three columns."""
_SYNC_URL_JS = ClientsideScript(Path(__file__).with_name("sync_cards_url.js"))


class ProjectCards:
    """Page tag: marks a component id as belonging to the project page's card grids."""


class CardGrid(StrEnum):
    """A grid of cards on a project's page, by the prefix of its query parameters and its ids."""

    EXPERIMENTS = "exps"
    COMPARISONS = "cmps"

    @property
    def filter_id(self) -> ValueId[ProjectCards]:
        return ValueId(f"{self}-filter")

    @property
    def pager_id(self) -> ValueId[ProjectCards]:
        return ValueId(f"{self}-pager")

    @property
    def list_id(self) -> DivId[ProjectCards]:
        return DivId(f"{self}-list")

    @property
    def count_id(self) -> DivId[ProjectCards]:
        return DivId(f"{self}-count")


class CardQuery(BaseModel, frozen=True, extra="forbid"):
    """Which cards a grid shows: those whose name has `q` in it, and which page of them."""

    q: str = ""
    page: int = Field(default=1, ge=1)

    @classmethod
    def of(cls, grid: CardGrid, query: Mapping[str, str]) -> CardQuery:
        """What a page URL's query asks `grid` to show; a malformed one just means the first page, unfiltered."""
        try:
            return cls(q=query.get(f"{grid}_q", "").strip(), page=int(query.get(f"{grid}_page", 1)))
        except (ValidationError, ValueError):
            return cls()


class Cards(typing.NamedTuple):
    """A page of cards, and how many there are in all."""

    total: int
    cards: list[Component]


@dataclass(frozen=True)
class CardSource:
    """What a grid shows: `read` draws the cards of a page (`name_contains`, `offset`) of a project's, and their total."""

    grid: CardGrid
    label: str
    read: Callable[[DataStore[...], int, str, int], Cards]
    empty: Callable[[int, CardQuery], Component | None]
    """What shows when no card matches: how to make the first one, say, or that the filter matched none."""


def _page(source: CardSource, store: DataStore[...], project_id: int, query: CardQuery) -> tuple[Cards, int]:
    """The page `query` asks for (the last one, if it asks past the end), and which page that is."""
    found = source.read(store, project_id, query.q, (query.page - 1) * PAGE_SIZE)
    last = max(1, math.ceil(found.total / PAGE_SIZE))
    if query.page <= last:
        return found, query.page
    return source.read(store, project_id, query.q, (last - 1) * PAGE_SIZE), last


def _drawn(source: CardSource, project_id: int, query: CardQuery, found: Cards, page: int) -> tuple[Any, ...]:
    """What a redraw writes: the cards (or what shows for none), the count, the pager's props, and the filter's style."""
    pager = pager_props(max(1, math.ceil(found.total / PAGE_SIZE)), page)
    shown = (
        dmc.SimpleGrid(found.cards, cols={"base": 1, "sm": 2, "lg": 3}, spacing="md")
        if found.cards
        else source.empty(project_id, query)
    )
    filtering = {"display": "block" if found.total > PAGE_SIZE or query.q else "none"}
    count = section_label(f"{source.label} · {found.total}")
    return shown, count, pager["total"], pager["value"], pager["style"], filtering


def card_grid(
    source: CardSource, store: DataStore[...], project_id: int, query: CardQuery
) -> Component | None:
    """`source`'s grid of a project's cards, showing what `query` asks for; nothing, if it has nothing to show or say."""
    found, page = _page(source, store, project_id, query)
    shown, count, total, value, pager_style, filter_style = _drawn(source, project_id, query, found, page)
    if shown is None:
        return None
    grid = source.grid
    return dmc.Box(
        [
            dmc.Group(
                [
                    dmc.Box(count, id=grid.count_id),
                    dmc.TextInput(
                        id=grid.filter_id,
                        value=query.q,
                        placeholder="Filter by name",
                        leftSection=icon(Icon.SEARCH),
                        size="xs",
                        w=220,
                        debounce=300,
                        style=filter_style,
                        **typing.cast("dict[str, Any]", {"aria-label": f"Filter {source.label.lower()}"}),
                    ),
                ],
                justify="space-between",
                mb="xs",
            ),
            dmc.Box(shown, id=grid.list_id),
            dmc.Pagination(
                id=grid.pager_id,
                total=total,
                value=value,
                withEdges=False,
                size="sm",
                mt="md",
                style=pager_style,
            ),
        ],
        mb="lg",
    )


def register_card_grid(app: Dash, source: CardSource) -> None:
    """Draw the page of `source`'s cards its filter and pager ask for, and keep them in the page's URL."""
    grid = source.grid

    @app.callback(
        Output(grid.list_id, "children", allow_duplicate=True),
        Output(grid.count_id, "children", allow_duplicate=True),
        Output(grid.pager_id, "total", allow_duplicate=True),
        Output(grid.pager_id, "value", allow_duplicate=True),
        Output(grid.pager_id, "style", allow_duplicate=True),
        Output(grid.filter_id, "style", allow_duplicate=True),
        Input(grid.filter_id, "value"),
        Input(grid.pager_id, "value"),
        State(constants.STATE_PROJECT_ID, "data"),
        prevent_initial_call=True,
    )
    def redraw(q: str | None, page: int | None, project_id: int) -> tuple[Any, ...]:
        # A new filter starts again from the first page; only the pager itself keeps its place.
        query = CardQuery(q=(q or "").strip(), page=(page or 1) if ctx.triggered_id == grid.pager_id else 1)
        return redraw_cards(source, get_data_store(), project_id, query)

    app.clientside_callback(
        _SYNC_URL_JS.source,
        Output(grid.filter_id, "className"),
        Input(grid.filter_id, "value"),
        Input(grid.pager_id, "value"),
        State(grid.filter_id, "id"),
        prevent_initial_call=True,
    )


def redraw_cards(
    source: CardSource, store: DataStore[...], project_id: int, query: CardQuery
) -> tuple[Any, ...]:
    """What a callback writes to draw `query`'s page of `source`'s cards again: its outputs, in `register_card_grid`'s order."""
    found, page = _page(source, store, project_id, query)
    return _drawn(source, project_id, query, found, page)
