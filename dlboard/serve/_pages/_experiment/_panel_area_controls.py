"""
How the viewer browses the panel area: paging and filtering it, opening panels, switching tabs.

The callbacks behind the panel area's own controls (`_panel_view.py` renders them). None of this edits what
a view *is* -- which panels are open and which tab is active are written without branching, and the paging
lives only in the URL and a cookie.
"""

from __future__ import annotations

import typing
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

from dash import Dash, Input, Output, State, ctx, no_update
from dash.exceptions import PreventUpdate
from pydantic import ValidationError
from structlog.stdlib import get_logger

from dlboard.serve import ClientsideScript, get_data_store
from dlboard.serve import _constants as constants
from dlboard.serve._pages._experiment._ids import (
    PANEL_PAGING_ID,
    STATE_PAGE_STORAGE,
)
from dlboard.serve._pages._experiment._page_model import (
    BasicExperimentPage,
)
from dlboard.serve._pages._experiment._page_repository import (
    load_page,
    ref_of,
)
from dlboard.serve._pages._experiment._paging import (
    SIZES_COOKIE,
    PageSizes,
    Paging,
    next_chart_paging,
    next_panel_paging,
    visible_chart_indexes,
    visible_open_panels,
)
from dlboard.serve._pages._experiment._panel_data import panel_frames
from dlboard.serve._pages._experiment._panel_view import (
    CHART_FILTER,
    CHART_PAGE_SIZE_ID,
    CHART_PAGER,
    CHART_PAGER_SLOT,
    LOADED_PANELS_STORE_ID,
    PANEL_ACCORDION,
    PANEL_AREA_ID,
    PANEL_CONTENT,
    PANEL_FILTER_ID,
    PANEL_PAGE_SIZE_ID,
    PANEL_PAGER_ID,
    PANEL_TABS_ID,
    RENAME_TAB_BUTTON_ID,
    chart_pager,
    clamped_paging,
    panel_area_children,
    render_panel_content,
    tab_from_component_value,
)

if TYPE_CHECKING:
    from dash.development.base_component import Component


_log = get_logger(__name__)

_ACTIVE_TAB_DISABLES_RENAME_JS = ClientsideScript(Path(__file__).with_name("active_tab_disables_rename.js"))
_PAGING_SIDE_EFFECTS_JS = ClientsideScript(Path(__file__).with_name("paging_side_effects.js"))

_SIZES_COOKIE_MAX_AGE: typing.Final = 365 * 24 * 60 * 60


class _PageChartsCtx(TypedDict):
    filter_ids: list[dict[str, str]]
    pager_ids: list[dict[str, str]]
    slot_ids: list[dict[str, str]]
    content_ids: list[dict[str, str]]
    loaded: list[str] | None
    paging_json: str
    page_json: str
    experiment_id: int


class _PagePanelsCtx(TypedDict):
    q: str | None
    size: str | None
    chart_size: str | None
    tabs_value: str | None
    paging_json: str
    page_json: str
    experiment_id: int
    rendered_accordions: list[dict[str, str]]


def _register_paging(app: Dash) -> None:
    """Which page of panels is showing: the filter, pagers, page size and tab, and the address bar mirroring them."""

    @app.callback(
        Output(PANEL_AREA_ID, "children"),
        Output(PANEL_PAGING_ID, "data", allow_duplicate=True),
        Output(LOADED_PANELS_STORE_ID, "data", allow_duplicate=True),
        Input(PANEL_PAGER_ID, "value", allow_optional=True),
        {
            "q": Input(PANEL_FILTER_ID, "value", allow_optional=True),
            "size": Input(PANEL_PAGE_SIZE_ID, "value", allow_optional=True),
            "chart_size": Input(CHART_PAGE_SIZE_ID, "value", allow_optional=True),
            "tabs_value": Input(PANEL_TABS_ID, "value", allow_optional=True),
            "paging_json": State(PANEL_PAGING_ID, "data"),
            "page_json": State(STATE_PAGE_STORAGE, "data"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            "rendered_accordions": State(PANEL_ACCORDION.all(), "id"),
        },
        prevent_initial_call=True,
        running=[(Output(PANEL_AREA_ID, "style"), {"opacity": 0.5}, {"opacity": 1})],
    )
    def page_panels(
        pager_value: int | None, panel_ctx: _PagePanelsCtx
    ) -> tuple[list[Component], str, list[str]]:
        """Show the page of panels a filter, pager, page size or tab change asks for."""
        paging = Paging.model_validate_json(panel_ctx["paging_json"])
        rendered_accordions = panel_ctx["rendered_accordions"]
        rendered_tab = rendered_accordions[0]["tab"] if rendered_accordions else ""
        tabs_value = panel_ctx["tabs_value"]
        tab = tab_from_component_value(tabs_value) if tabs_value else rendered_tab
        try:
            sizes = PageSizes.model_validate(
                {
                    "panels": int(panel_ctx["size"] or paging.sizes.panels),
                    "charts": int(panel_ctx["chart_size"] or paging.sizes.charts),
                }
            )
        except (ValueError, ValidationError):
            raise PreventUpdate from None

        requested = next_panel_paging(
            paging,
            q=panel_ctx["q"] or "",
            pager_value=pager_value,
            size=sizes.panels,
            chart_size=sizes.charts,
            tab_changed=tab != rendered_tab,
        )
        # Not `STATE_VIEW_ID`, for the same reason as `render_opened_panels`' own `ref_of`.
        store = get_data_store()
        page = load_page(
            store,
            ref_of(
                panel_ctx["experiment_id"], BasicExperimentPage.model_validate_json(panel_ctx["page_json"])
            ),
        )
        new_paging = clamped_paging(page, tab, requested.viewing(tab=tab)).normalized(page.panels)
        # A control mounting reports its current value back as a "change" -- nothing to do for that.
        if new_paging == paging and tab == rendered_tab:
            raise PreventUpdate
        if new_paging.sizes != paging.sizes:
            # Dash's `has_context` wrapper hides `response`'s property type from pyrefly.
            cast("Any", ctx).response.set_cookie(
                SIZES_COOKIE,
                new_paging.sizes.model_dump_json(),
                max_age=_SIZES_COOKIE_MAX_AGE,
                samesite="Lax",
            )
        open_panels = visible_open_panels(page.panels, tab, new_paging)
        return (
            panel_area_children(panel_frames(store, page, new_paging, open_panels), page, tab, new_paging),
            new_paging.model_dump_json(),
            open_panels,
        )

    @app.callback(
        Output(CHART_PAGER_SLOT.all(), "children"),
        Output(PANEL_CONTENT.all(), "children", allow_duplicate=True),
        Output(PANEL_PAGING_ID, "data", allow_duplicate=True),
        Input(CHART_FILTER.all(), "value"),
        Input(CHART_PAGER.all(), "value"),
        {
            "filter_ids": State(CHART_FILTER.all(), "id"),
            "pager_ids": State(CHART_PAGER.all(), "id"),
            "slot_ids": State(CHART_PAGER_SLOT.all(), "id"),
            "content_ids": State(PANEL_CONTENT.all(), "id"),
            "loaded": State(LOADED_PANELS_STORE_ID, "data"),
            "paging_json": State(PANEL_PAGING_ID, "data"),
            "page_json": State(STATE_PAGE_STORAGE, "data"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
        },
        prevent_initial_call=True,
        running=[(Output(PANEL_AREA_ID, "style"), {"opacity": 0.5}, {"opacity": 1})],
    )
    def page_panel_charts(
        filter_values: list[str | None], pager_values: list[int], chart_ctx: _PageChartsCtx
    ) -> tuple[list[Any], list[Any], str]:
        """Show the page of a panel's charts that its filter or pager asks for; the other panels are untouched."""
        filter_ids, pager_ids = chart_ctx["filter_ids"], chart_ctx["pager_ids"]
        slot_ids, content_ids = chart_ctx["slot_ids"], chart_ctx["content_ids"]
        paging = Paging.model_validate_json(chart_ctx["paging_json"])
        requested = next_chart_paging(
            paging,
            filters={pid["panel"]: value or "" for pid, value in zip(filter_ids, filter_values, strict=True)},
            pagers={pid["panel"]: value for pid, value in zip(pager_ids, pager_values, strict=True)},
        )
        # Controls mounting report their current value back as a "change" -- nothing to do for that.
        if requested == paging:
            raise PreventUpdate

        experiment_id = chart_ctx["experiment_id"]
        store = get_data_store()
        page = load_page(
            store, ref_of(experiment_id, BasicExperimentPage.model_validate_json(chart_ctx["page_json"]))
        )
        panel_by_name = {p.name: p for p in page.panels}
        changed = {
            name
            for name in requested.charts.keys() | paging.charts.keys()
            if requested.chart_paging(name) != paging.chart_paging(name) and name in panel_by_name
        }
        for name in changed:
            chart_paging = requested.chart_paging(name)
            number = visible_chart_indexes(panel_by_name[name], requested).number
            requested = requested.with_charts(name, chart_paging.model_copy(update={"page": number}))

        redrawn = changed & set(chart_ctx["loaded"] or [])
        frames = panel_frames(store, page, requested, redrawn)
        return (
            [
                chart_pager(
                    panel_by_name[pid["panel"]], visible_chart_indexes(panel_by_name[pid["panel"]], requested)
                )
                if pid["panel"] in changed
                else no_update
                for pid in slot_ids
            ],
            [
                render_panel_content(frames, panel_by_name[pid["panel"]], requested)
                if pid["panel"] in redrawn
                else no_update
                for pid in content_ids
            ],
            requested.model_dump_json(),
        )

    # Mirrors the paging into the address bar so a page or filter can be bookmarked and shared, and scrolls
    # a flipped page back to its top -- purely client-side, see `paging_side_effects.js`. Runs once on load
    # too: the paging rendered can differ from the URL's (a page that no longer exists, a `?chart=` link).
    app.clientside_callback(
        _PAGING_SIDE_EFFECTS_JS.source,
        Output(PANEL_PAGING_ID, "data", allow_duplicate=True),
        Input(PANEL_PAGING_ID, "data"),
        prevent_initial_call="initial_duplicate",
    )


def register_panel_area_callbacks(app: Dash) -> None:
    """Paging, opening panels and switching tabs."""
    _register_paging(app)

    @app.callback(
        Output(PANEL_PAGING_ID, "data", allow_duplicate=True),
        Input(PANEL_ACCORDION.all(), "value"),
        State(PANEL_PAGING_ID, "data"),
        State(STATE_PAGE_STORAGE, "data"),
        State(PANEL_CONTENT.all(), "id"),
        prevent_initial_call=True,
    )
    def remember_open_panels(
        open_values: list[list[str] | None],
        paging_json: str,
        page_json: str,
        shown_panels: list[dict[str, str]],
    ) -> str:
        """Record which panels are open in the paging -- and so the URL -- never in the page itself."""
        paging = Paging.model_validate_json(paging_json)
        panels = BasicExperimentPage.model_validate_json(page_json).panels
        # Only the panels on screen are in `open_values`; the open state of every other panel (on another
        # page or tab) is carried over untouched.
        shown = {pid["panel"] for pid in shown_panels}
        was_open = paging.opened(panels)
        now_open = [name for name in was_open if name not in shown] + [
            name for group in open_values for name in (group or [])
        ]
        # The accordion's `value` mounting with whatever `accordion_view` just initialized it to (every
        # render, including ones that aren't about panels being open at all) reports back here as if the
        # user had just opened/closed something -- nothing to record for that.
        if set(now_open) == set(was_open):
            raise PreventUpdate
        return paging.viewing(open_panels=now_open).normalized(panels).model_dump_json()

    @app.callback(
        Output(PANEL_CONTENT.all(), "children"),
        Output(LOADED_PANELS_STORE_ID, "data"),
        Input(PANEL_ACCORDION.all(), "value"),
        State(PANEL_CONTENT.all(), "id"),
        State(LOADED_PANELS_STORE_ID, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(STATE_PAGE_STORAGE, "data"),
        State(PANEL_PAGING_ID, "data"),
        prevent_initial_call=True,
    )
    def render_opened_panels(  # noqa: PLR0913
        open_values: list[list[str] | None],
        panel_ids: list[dict[str, str]],
        loaded: list[str] | None,
        experiment_id: int,
        page_json: str,
        paging_json: str,
    ) -> tuple[list[Any], list[str]]:
        open_set = {name for group in open_values for name in (group or [])}
        loaded_set = set(loaded or [])
        newly_opened = open_set - loaded_set
        if not newly_opened:
            raise PreventUpdate

        paging = Paging.model_validate_json(paging_json)
        store = get_data_store()
        # `ref_of`, not `STATE_VIEW_ID`: this accordion
        # remount can itself be the tail end of an edit that just branched into a new view.
        curr_page = BasicExperimentPage.model_validate_json(page_json)
        page = load_page(store, ref_of(experiment_id, curr_page))
        panel_by_name = {p.name: p for p in page.panels}
        rendered = newly_opened & panel_by_name.keys()
        frames = panel_frames(store, page, paging, rendered)
        outputs = [
            render_panel_content(frames, panel_by_name[pid["panel"]], paging)
            if pid["panel"] in rendered
            else no_update
            for pid in panel_ids
        ]
        return outputs, sorted(loaded_set | newly_opened)

    # Tab switching is entirely client-side (Mantine's own state, no server round trip) -- so
    # whether "Rename the active tab" should be disabled (on the ungrouped "General" tab) has to
    # update client-side too, not just from this render's initial `disabled=`.
    app.clientside_callback(
        _ACTIVE_TAB_DISABLES_RENAME_JS.source,
        Output(RENAME_TAB_BUTTON_ID, "disabled"),
        Input(PANEL_TABS_ID, "value"),
    )
