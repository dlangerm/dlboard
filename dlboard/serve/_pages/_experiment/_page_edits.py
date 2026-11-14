"""
An edit to an experiment page, and the one way to make it: apply, save, and re-render the panel area.

Or, for a change to only the data the charts draw (a run drawn or hidden, new points logged), redraw in place just the
charts whose data changed (`ChartsOnScreen`).

Every feature's edit callback works out a `PageEdit` -- what it does to the panels, the settings it writes, and
what has to be on screen afterwards -- and hands it to `commit` (or `save_edit`, for an edit the browser has
already drawn itself). The edit is always applied to the page as stored, found by the one the browser holds,
never to the browser's copy of it: a copy can be stale, and writing it back would undo whatever happened since.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from dash import State, no_update
from pydantic import Json
from structlog.stdlib import get_logger

from dlboard.serve import get_data_store
from dlboard.serve._pages._dash_helpers import CallbackStates
from dlboard.serve._pages._experiment import _chart_view as chart_view
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _panel_data as panel_data
from dlboard.serve._pages._experiment._focus import focus_banner
from dlboard.serve._pages._experiment._page_model import BasicExperimentPage
from dlboard.serve._pages._experiment._page_repository import load_page, ref_of, save_page
from dlboard.serve._pages._experiment._paging import Paging, reveal, visible_open_panels
from dlboard.serve._pages._experiment._panel_data import panel_frames
from dlboard.serve._pages._experiment._panel_view import accordion_view, resolved_paging
from dlboard.serve._pages._experiment._shown_runs import page_scope

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from dash import html

    from dlboard import models
    from dlboard.models import DataStore
    from dlboard.serve._pages._experiment._page_model import DocumentSettings

_log = get_logger(__name__)

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


def render_panel_area(
    store: DataStore[...], page: BasicExperimentPage, paging: Paging, *, focus_chart: str | None = None
) -> html.Div:
    """The panel area of `page`: the tabs and the page of panels `paging` shows, with the data of the charts open in them."""
    paging = resolved_paging(page, paging, focus_chart=focus_chart)
    open_panels = visible_open_panels(page.panels, paging.shown_tab(page.panels), paging)
    return accordion_view(
        panel_frames(store, page, paging, open_panels), page, paging, focus_banner(store, page, paging)
    )


def commit(page_json: str, paging_json: str, edit: PageEdit) -> tuple[BasicExperimentPage, html.Div]:
    """Apply and save `edit`, then re-render the whole panel area from the saved page, with `edit.reveal` on screen."""
    before, saved = _apply(page_json, edit)
    paging = Paging.model_validate_json(paging_json).viewing(tab=edit.tab, open_panels=edit.open_panels)
    if edit.reveal is not None:
        paging = _revealed(before, saved, paging, edit.reveal)
    return saved, render_panel_area(get_data_store(), saved, paging)


def refresh(page_json: str, paging_json: str) -> tuple[BasicExperimentPage, html.Div]:
    """
    Re-render the whole panel area from the page as stored, saving nothing.

    For a change to what the page draws that is not an edit of the page: a comparison gaining or losing a run changes
    the data of every chart, but not the page, so it must not fork a copy of it for someone who is not its owner.
    """
    store = get_data_store()
    stored = load_page(store, ref_of(BasicExperimentPage.model_validate_json(page_json)))
    return stored, render_panel_area(store, stored, Paging.model_validate_json(paging_json))


class EditCtx(CallbackStates):
    """The `State`s every page edit reads: the page and paging the browser holds. Each edit's own adds what it needs."""

    page_json: Annotated[str, State(ids.STATE_PAGE_STORAGE, "data")]
    paging_json: Annotated[str, State(ids.PANEL_PAGING_ID, "data")]


def fetch_open_panel_dataframes(
    store: DataStore[...],
    page: BasicExperimentPage,
    chart_ids: list[chart_view.ChartID],
    paging: Paging,
) -> tuple[dict[str, panel_data.PanelData], bool]:
    """
    Fetch the dataframe of each panel with charts on screen, for just those charts -- once per panel.

    Not once per chart: "Auto-generate charts" routinely groups several charts sharing a
    panel, so fetching per panel (rather than per chart) keeps a redraw's cost proportional to how many
    panels are open. And not for the panel's other charts: only one page of them is on screen (see
    `Paging`), so a redraw costs what that page does, however many charts the panel holds. Of the runs the page draws
    (`page_scope`): an experiment's shown runs, or a comparison's but the ones the viewer hid.
    """
    scope = page_scope(store, page, paging.hidden, paging.focus)
    names = panel_data.experiment_names(store, scope)
    panels_by_name = {p.name: p for p in page.panels}
    mounted: dict[str, set[int]] = {}
    for chart_id in chart_ids:
        if chart_id.index is not None:
            mounted.setdefault(chart_id.panel, set()).add(chart_id.index)
    dataframes: dict[str, panel_data.PanelData] = {}
    reads = panel_data.ExperimentReads(store)  # its metric names and run names, read once for every panel
    ok = True
    for panel_name, indexes in mounted.items():
        panel = panels_by_name.get(panel_name)
        if panel is None:
            continue
        shown = panel.model_copy(update={"charts": [c for i, c in enumerate(panel.charts) if i in indexes]})
        try:
            dataframes[panel_name] = panel_data.scope_panel_data(store, scope, shown, names, reads)
        except Exception:  # noqa: BLE001 -- one bad panel must not break the whole redraw
            _log.exception("Failed to fetch panel %r for a redraw", panel_name)
            ok = False
    return dataframes, ok


def refresh_chart_bodies(
    chart_ids: list[chart_view.ChartID],
    panels_by_name: dict[str, models.PanelInstance[Any, Any]],
    dataframes: dict[str, panel_data.PanelData],
    shown: dict[tuple[str, int | None], str],
    page_size: int,
) -> tuple[list[Any], bool]:
    """
    The new body of each chart whose data is no longer what its on-screen body was drawn from.

    `shown` is the `chart_data_fingerprint` each chart's body carries (`chart_body`); one that still
    matches what the data says now is left alone (`no_update`).
    """
    contents: list[Any] = []
    ok = True
    for chart_id in chart_ids:
        panel_name, index = chart_id.panel, chart_id.index
        panel = panels_by_name.get(panel_name)
        data = dataframes.get(panel_name)
        if panel is None or data is None or index is None or index >= len(panel.charts):
            contents.append(no_update)
            continue
        try:
            frame = data.for_chart(panel.charts[index])
            if panel_data.chart_data_fingerprint(frame) == shown.get((panel_name, index)):
                contents.append(no_update)
            else:
                contents.append(
                    chart_view.chart_body(
                        panel, index, frame, page_size, wide=data.wide_history(panel.charts[index])
                    )
                )
        except Exception:  # noqa: BLE001 -- one bad chart must not break the whole redraw
            _log.exception("Failed to redraw chart %r", f"{panel_name}:{index}")
            contents.append(no_update)
            ok = False
    return contents, ok


class ChartsOnScreen(CallbackStates):
    """
    The charts on screen and the data each was drawn from: what is needed to redraw them in place.

    For a change to the data the charts draw that leaves the page's panels as they are -- new points, or a run drawn or
    hidden. The panel area stays mounted, so nothing flashes and no spinner covers it (`commit` and `refresh` draw it
    again whole), and only the charts whose own data changed are drawn again. A callback that redraws in place lists
    these `State`s and `Output(chart_view.CHART_BODY.all(), "children")`.
    """

    chart_ids: Annotated[list[chart_view.ChartID], State(chart_view.CHART_BODY.all(), "id")]
    fingerprint_ids: Annotated[list[chart_view.ChartID], State(chart_view.CHART_FINGERPRINT.all(), "id")]
    fingerprints: Annotated[list[str], State(chart_view.CHART_FINGERPRINT.all(), "children")]
    hiding: Annotated[
        list[Json[list[int]]], State(chart_view.CHART_FINGERPRINT.all(), chart_view.HIDING_RUNS)
    ]
    """Which runs each chart hides in the browser: one that hides any is drawn again, as it is not what it was drawn as."""

    def unchanged(self) -> list[Any]:
        """The value of `CHART_BODY.all()`'s `children` that leaves every chart as it is."""
        return [no_update] * len(self.chart_ids)

    def redraw(
        self, store: DataStore[...], page: BasicExperimentPage, paging: Paging
    ) -> tuple[list[Any], bool]:
        """A new body for each chart on screen whose data, as `page` and `paging` draw it, changed; and whether all read."""
        dataframes, fetched = fetch_open_panel_dataframes(store, page, self.chart_ids, paging)
        shown = {
            (i.panel, i.index): fingerprint
            for i, fingerprint, hiding in zip(
                self.fingerprint_ids, self.fingerprints, self.hiding, strict=True
            )
            if not hiding
        }
        contents, drawn = refresh_chart_bodies(
            self.chart_ids, {p.name: p for p in page.panels}, dataframes, shown, paging.sizes.charts
        )
        return contents, fetched and drawn
