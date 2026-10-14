"""
Edit a page's panels or settings, save it (branching into a view of your own if it isn't yours), and re-render.

What every feature's edit callback does once it has worked out the edit; see `_page_repository.save_page`
for the branching.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

from structlog.stdlib import get_logger

from dlboard.serve import get_data_store
from dlboard.serve._pages._experiment._page_model import BasicExperimentPage
from dlboard.serve._pages._experiment._page_repository import (
    PageRef,
    persist_settings,
    save_page,
)
from dlboard.serve._pages._experiment._paging import (
    Paging,
    reveal,
)
from dlboard.serve._pages._experiment._panel_data import panel_frames
from dlboard.serve._pages._experiment._panel_view import accordion_view

if TYPE_CHECKING:
    from collections.abc import Callable

    from dash import html

    from dlboard import models
    from dlboard.models import DataStore

_log = get_logger(__name__)


def persist_settings_and_rerender(
    store: DataStore[...],
    ref: PageRef,
    paging_json: str,
    updates: dict[str, Any],
) -> tuple[BasicExperimentPage, html.Div]:
    """Merge `updates` into page_settings (server-authoritative), persist, and re-render the accordion."""
    page = persist_settings(store, ref, updates)
    return page, accordion_view(panel_frames(store, page), page, Paging.model_validate_json(paging_json))


def mutate_panels(
    page_json: str,
    mutate: Callable[[list[models.PanelInstance[Any, Any]]], list[models.PanelInstance[Any, Any]]],
    *,
    extra_settings: dict[str, Any] | None = None,
) -> BasicExperimentPage:
    """
    Load page from client-cached state, apply `mutate` to its panels, and persist -- without re-rendering.

    For an edit the browser has already applied itself (see `panel_grid_columns.js`); `mutate_panels_and_rerender`
    is for every other one.

    `extra_settings` merges into `page_settings` alongside the panel mutation -- e.g. moving a chart
    or panel to a different tab also switches `ACTIVE_TAB_KEY` to it, in the same page update, rather
    than leaving the user looking at the tab they dragged *from*.

    Always branches into a view of your own first if this page isn't already one of your own -- see
    `save_page`. A panel/chart structure edit is never "just viewing", unlike `persist_settings`'s
    two opt-outs, so there's no equivalent flag here.
    """
    curr_page = BasicExperimentPage.model_validate_json(page_json)
    updates: dict[str, Any] = {"panels": mutate(curr_page.panels)}
    if extra_settings:
        updates["page_settings"] = {**curr_page.page_settings, **extra_settings}
    return save_page(get_data_store(), curr_page.model_copy(update=updates))


def chart_id_at(page_json: str, panel_name: str, index: int) -> str | None:
    """The `ChartInstance.id` of the chart at `index` in the panel named `panel_name`, if there is one."""
    page = BasicExperimentPage.model_validate_json(page_json)
    panel = next((p for p in page.panels if p.name == panel_name), None)
    return panel.charts[index].id if panel is not None and index < len(panel.charts) else None


def mutate_panels_and_rerender(  # noqa: PLR0913
    page_json: str,
    paging_json: str,
    mutate: Callable[[list[models.PanelInstance[Any, Any]]], list[models.PanelInstance[Any, Any]]],
    *,
    extra_settings: dict[str, Any] | None = None,
    reveal_panel: str | None = None,
    reveal_chart: str | None = None,
) -> tuple[BasicExperimentPage, html.Div]:
    """
    `mutate_panels`, then re-render the whole accordion from the saved page.

    The saved page is a branch into a view of your own first if this page isn't already one of your
    own -- see `save_page`. `reveal_panel` names a panel, and `reveal_chart` (a `ChartInstance.id`) a chart,
    that the edit just made or moved and that must be on screen afterwards, whichever page of panels
    or charts it landed on (see `reveal`).
    """
    saved = mutate_panels(page_json, mutate, extra_settings=extra_settings)
    paging = Paging.model_validate_json(paging_json)
    holder = next((p.name for p in saved.panels if any(c.id == reveal_chart for c in p.charts)), None)
    if reveal_chart is not None and holder is not None:
        paging = reveal(saved.panels, paging, panel_name=holder, chart_id=reveal_chart)
    elif reveal_panel is not None:
        paging = reveal(saved.panels, paging, panel_name=reveal_panel)
    return saved, accordion_view(panel_frames(get_data_store(), saved), saved, paging)


class EditCtx(TypedDict):
    """
    Base for the grouped ("flexible callback signature") `State` dicts panel/chart mutations take.

    One dict argument instead of several separate ones keeps those callbacks under ruff's
    `max-args`; each subclass adds whatever that one callback needs next to `page_json`.
    """

    page_json: str
    paging_json: str
