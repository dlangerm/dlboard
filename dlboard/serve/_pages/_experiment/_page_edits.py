"""
An edit to an experiment page, and the one way to make it: apply, save, and re-render the panel area.

Every feature's edit callback works out a `PageEdit` -- what it does to the panels, the settings it writes, and
what has to be on screen afterwards -- and hands it to `commit` (or `save_edit`, for an edit the browser has
already drawn itself). The edit is always applied to the page as stored, found by the one the browser holds,
never to the browser's copy of it: a copy can be stale, and writing it back would undo whatever happened since.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from dash import State

from dlboard.serve import get_data_store
from dlboard.serve._pages._dash_helpers import CallbackStates
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment._focus import focus_banner
from dlboard.serve._pages._experiment._page_model import BasicExperimentPage
from dlboard.serve._pages._experiment._page_repository import load_page, ref_of, save_page
from dlboard.serve._pages._experiment._paging import Paging, reveal, visible_open_panels
from dlboard.serve._pages._experiment._panel_data import panel_frames
from dlboard.serve._pages._experiment._panel_view import accordion_view, resolved_paging

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from dash import html

    from dlboard import models
    from dlboard.serve._pages._experiment._page_model import DocumentSettings

type PanelsEdit = Callable[[list[models.PanelInstance[Any, Any]]], list[models.PanelInstance[Any, Any]]]


@dataclass(frozen=True)
class RevealPanel:
    """Show the page of panels the panel named `name` is on."""

    name: str


@dataclass(frozen=True)
class RevealChart:
    """Show the panel holding the chart `chart_id` (a `ChartInstance.id`), on the page of its charts it is on."""

    chart_id: str


@dataclass(frozen=True)
class RevealChartAt:
    """`RevealChart` for whichever chart was at `index` in the panel `panel` *before* the edit moved it."""

    panel: str
    index: int


type Reveal = RevealPanel | RevealChart | RevealChartAt


@dataclass(frozen=True)
class PageEdit:
    """
    One edit to a page -- to its panels, its settings, or both -- and how it should be shown afterwards.

    `tab` and `open_panels` are not saved: they change only the `Paging` the panel area is re-rendered
    with (and so the URL), like `reveal`.
    """

    panels: PanelsEdit | None = None
    document: DocumentSettings | None = None
    tab: str | None = None
    open_panels: Sequence[str] | None = None
    reveal: Reveal | None = None


def _apply(page_json: str, edit: PageEdit) -> tuple[BasicExperimentPage, BasicExperimentPage]:
    """The stored page `page_json` names, and that page with `edit` applied and saved (branching if it isn't yours)."""
    store = get_data_store()
    held = BasicExperimentPage.model_validate_json(page_json)
    before = load_page(store, ref_of(held))
    edited = (
        before if edit.panels is None else before.model_copy(update={"panels": edit.panels(before.panels)})
    )
    if edit.document is not None:
        edited = edited.with_settings(edit.document)
    return before, save_page(store, edited)


def save_edit(page_json: str, edit: PageEdit) -> BasicExperimentPage:
    """Apply and save `edit` without re-rendering -- for an edit the browser has already drawn (`panel_grid_columns.js`)."""
    return _apply(page_json, edit)[1]


def _revealed(
    before: BasicExperimentPage, saved: BasicExperimentPage, paging: Paging, target: Reveal
) -> Paging:
    match target:
        case RevealPanel(name=name):
            return reveal(saved.panels, paging, panel_name=name)
        case RevealChartAt(panel=panel_name, index=index):
            panel = next((p for p in before.panels if p.name == panel_name), None)
            if panel is None or not 0 <= index < len(panel.charts):
                return paging
            return _revealed(before, saved, paging, RevealChart(panel.charts[index].id))
        case RevealChart(chart_id=chart_id):
            holder = next((p.name for p in saved.panels if any(c.id == chart_id for c in p.charts)), None)
            return (
                paging
                if holder is None
                else reveal(saved.panels, paging, panel_name=holder, chart_id=chart_id)
            )


def commit(page_json: str, paging_json: str, edit: PageEdit) -> tuple[BasicExperimentPage, html.Div]:
    """Apply and save `edit`, then re-render the whole panel area from the saved page, with `edit.reveal` on screen."""
    before, saved = _apply(page_json, edit)
    paging = Paging.model_validate_json(paging_json).viewing(tab=edit.tab, open_panels=edit.open_panels)
    if edit.reveal is not None:
        paging = _revealed(before, saved, paging, edit.reveal)
    paging = resolved_paging(saved, paging)
    open_panels = visible_open_panels(saved.panels, paging.shown_tab(saved.panels), paging)
    store = get_data_store()
    return saved, accordion_view(
        panel_frames(store, saved, paging, open_panels), saved, paging, focus_banner(store, saved, paging)
    )


class EditCtx(CallbackStates):
    """The `State`s every page edit reads: the page and paging the browser holds. Each edit's own adds what it needs."""

    page_json: Annotated[str, State(ids.STATE_PAGE_STORAGE, "data")]
    paging_json: Annotated[str, State(ids.PANEL_PAGING_ID, "data")]
