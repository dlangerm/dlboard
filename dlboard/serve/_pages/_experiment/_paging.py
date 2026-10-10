"""
Paging and filtering of the experiment page's panels and charts. Pure logic, no Dash.

An experiment can hold hundreds of panels and thousands of charts, and every chart on screen costs a
fetch and a render, so the page only ever shows one *page* of panels (of the active tab) and, inside
each panel, one page of its charts. Both levels take a fuzzy filter -- panels by name, charts by the
metric or artifact keys they show -- so finding one doesn't mean paging through the rest.

`Paging` is everything about "which slice is showing": it is the page URL's query (`?panels_q=`,
`?panels_page=`, `?charts=`, see `Paging.to_query`) plus the viewer's page sizes (a cookie, since the
server needs them for the very first render). Nothing here is ever saved to a view.
"""

from __future__ import annotations

import json
import math
import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, TypeAdapter, ValidationError

from dlboard.serve._pages._experiment import _dataframe_helpers as dfh

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from dlboard import models

PanelsPerPage = Literal[5, 10, 20, 50]
ChartsPerPage = Literal[6, 12, 24, 48]


def fuzzy_match(query: str, text: str) -> bool:
    """
    Whether every whitespace-separated token of `query` is a case-insensitive subsequence of `text`.

    So `"vl loss"` matches `"val/loss"` and `"train_loss"` alike; an empty query matches everything.
    """
    haystack = text.lower()
    return all(_is_subsequence(token, haystack) for token in query.lower().split())


def _is_subsequence(needle: str, haystack: str) -> bool:
    remaining = iter(haystack)
    return all(char in remaining for char in needle)  # `in` on an iterator consumes up to the match


class PageSizes(BaseModel, frozen=True, extra="ignore"):
    """How many panels per page, and charts per panel page, one viewer has chosen."""

    panels: PanelsPerPage = 10
    charts: ChartsPerPage = 12

    @classmethod
    def from_cookie(cls, raw: str | None) -> PageSizes:
        """Parse the `SIZES_COOKIE` value; a missing or malformed one just means the defaults."""
        try:
            return cls.model_validate_json(raw or "{}")
        except ValidationError:
            return cls()


class ChartPaging(BaseModel, frozen=True, extra="forbid"):
    """One panel's chart filter and which page of the filtered charts is showing."""

    q: str = ""
    page: int = Field(default=1, ge=1)


_CHARTS_ADAPTER: typing.Final = TypeAdapter(dict[str, ChartPaging])


def _load_json(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


class Paging(BaseModel, frozen=True, extra="ignore", populate_by_name=True):
    """Which slice of the experiment page is showing. See the module docstring."""

    panel_q: str = Field(default="", alias="panels_q")
    """Fuzzy filter on panel names, over the active tab's panels."""

    panel_page: int = Field(default=1, ge=1, alias="panels_page")

    charts: Annotated[dict[str, ChartPaging], BeforeValidator(_load_json)] = Field(default_factory=dict)
    """Per panel (by name): its chart filter and page. A panel absent here is unfiltered, on page 1."""

    sizes: PageSizes = Field(default_factory=PageSizes)

    @classmethod
    def from_request(cls, query: Mapping[str, str], sizes_cookie: str | None) -> Paging:
        """Parse a page request; a malformed link just shows the first page rather than breaking the page."""
        sizes = PageSizes.from_cookie(sizes_cookie)
        try:
            return cls.model_validate({**query, "sizes": sizes})
        except ValidationError:
            return cls(sizes=sizes)

    def to_query(self) -> dict[str, str]:
        """The URL query parameters that reproduce this, leaving out every default."""
        charts = {name: paging for name, paging in self.charts.items() if paging != ChartPaging()}
        query = {
            "panels_q": self.panel_q,
            "panels_page": str(self.panel_page) if self.panel_page > 1 else "",
            "charts": _CHARTS_ADAPTER.dump_json(charts, exclude_defaults=True).decode() if charts else "",
        }
        return {name: value for name, value in query.items() if value}

    def chart_paging(self, panel_name: str) -> ChartPaging:
        return self.charts.get(panel_name, ChartPaging())

    def with_panels(self, *, q: str | None = None, page: int | None = None) -> Paging:
        """A copy with the panel filter and/or page replaced; a new filter goes back to the first page."""
        return self.model_copy(
            update={
                "panel_q": self.panel_q if q is None else q,
                "panel_page": page if page is not None else (1 if q is not None else self.panel_page),
            }
        )

    def with_charts(self, panel_name: str, chart_paging: ChartPaging) -> Paging:
        """A copy with one panel's chart filter and page replaced."""
        return self.model_copy(update={"charts": {**self.charts, panel_name: chart_paging}})


@dataclass(frozen=True)
class Slice[T]:
    """One page of a sequence: its items, the (clamped) 1-based page number, and how many pages exist."""

    items: list[T]
    number: int
    total: int


def page_slice[T](items: Sequence[T], number: int, per_page: int) -> Slice[T]:
    """`items`' page `number`, clamped into range so a stale page number lands on the last page."""
    total = max(1, math.ceil(len(items) / per_page))
    number = min(max(number, 1), total)
    start = (number - 1) * per_page
    return Slice(list(items[start : start + per_page]), number, total)


def chart_search_keys(chart: models.ChartInstance[Any, Any]) -> set[str]:
    """
    What a chart's filter matches against: the metric and artifact keys it shows, and its chart type.

    Taken from the chart's `hint_required_*` methods rather than any one chart type's own settings,
    so it works for every chart plugin. A chart whose hint is `None` ("everything", like a table
    chart with no metric filter) is findable by its chart type alone.
    """
    columns = chart.hint_required_columns() or set()
    return {
        chart.chart_type,
        *(column for column in columns if column not in dfh.NO_DATA_COLUMNS),
        *(chart.hint_required_artifact_keys() or set()),
        *chart.hint_required_artifact_key_prefixes(),
    }


def chart_matches(chart: models.ChartInstance[Any, Any], query: str) -> bool:
    """Whether one of `chart`'s keys matches all of `query` -- never split across two keys."""
    return not query.strip() or any(fuzzy_match(query, key) for key in chart_search_keys(chart))


def visible_panels[P: models.PanelInstance[Any, Any]](
    panels: Sequence[P], tab: str, paging: Paging
) -> Slice[P]:
    """The page of `tab`'s panels `paging` shows, after its name filter."""
    matching = [p for p in panels if p.tab == tab and fuzzy_match(paging.panel_q, p.name)]
    return page_slice(matching, paging.panel_page, paging.sizes.panels)


def visible_chart_indexes(panel: models.PanelInstance[Any, Any], paging: Paging) -> Slice[int]:
    """
    The page of `panel`'s charts `paging` shows, after its filter, as indexes into `panel.charts`.

    Indexes rather than charts because everything addressing a chart (its component ids, edit and
    delete) goes by its position in the whole panel, which a filter or page must not change.
    """
    chart_paging = paging.chart_paging(panel.name)
    matching = [i for i, chart in enumerate(panel.charts) if chart_matches(chart, chart_paging.q)]
    return page_slice(matching, chart_paging.page, paging.sizes.charts)


def reveal(
    panels: Sequence[models.PanelInstance[Any, Any]],
    paging: Paging,
    *,
    panel_name: str,
    chart_id: str | None = None,
) -> Paging:
    """
    `paging`, moved to wherever puts `panel_name` (and `chart_id` within it) on screen.

    A filter that would hide the target is cleared. Only for the *position* -- opening the panel and
    switching to its tab is the page settings' business, not this. A name or id nothing matches leaves
    `paging` as it was.
    """
    panel = next((p for p in panels if p.name == panel_name), None)
    if panel is None:
        return paging
    if not fuzzy_match(paging.panel_q, panel.name):
        paging = paging.with_panels(q="")
    siblings = [p.name for p in panels if p.tab == panel.tab and fuzzy_match(paging.panel_q, p.name)]
    paging = paging.with_panels(page=siblings.index(panel.name) // paging.sizes.panels + 1)

    index = next((i for i, chart in enumerate(panel.charts) if chart.id == chart_id), None)
    if index is None:
        return paging
    query = paging.chart_paging(panel.name).q
    if not chart_matches(panel.charts[index], query):
        query = ""
    shown = [i for i, chart in enumerate(panel.charts) if chart_matches(chart, query)]
    return paging.with_charts(
        panel.name, ChartPaging(q=query, page=shown.index(index) // paging.sizes.charts + 1)
    )
