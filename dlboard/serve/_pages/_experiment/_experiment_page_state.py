"""
The panel area's own callbacks -- paging, which panels are open, the active tab -- and the live-update ids.

Also the "edit the panels, save, re-render" helpers every feature module builds on. The page model, its
persistence, its data and its render tree each have a module of their own (see the package docstring).
"""

from __future__ import annotations

import typing
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, ctx, html, no_update
from dash.exceptions import PreventUpdate
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from structlog.stdlib import get_logger

from dlboard.serve import ClientsideScript, get_data_store
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import DivId, IntervalId, StoreId, ValueId
from dlboard.serve._pages._experiment._ids import (
    PANEL_PAGING_ID,
    STATE_PAGE_STORAGE,
    ExperimentPage,
)
from dlboard.serve._pages._experiment._page_model import (
    ACTIVE_TAB_KEY,
    OPEN_PANEL_KEY,
    BasicExperimentPage,
)
from dlboard.serve._pages._experiment._page_repository import (
    PageRef,
    load_page,
    persist_settings,
    ref_of,
    save_page,
)
from dlboard.serve._pages._experiment._paging import (
    SIZES_COOKIE,
    PageSizes,
    Paging,
    next_chart_paging,
    next_panel_paging,
    reveal,
    visible_chart_indexes,
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
    accordion_view,
    chart_pager,
    clamped_paging,
    panel_area_children,
    render_panel_content,
    tab_from_component_value,
    visible_open_panels,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from dash.development.base_component import Component

    from dlboard import models
    from dlboard.models import DataStore

_log = get_logger(__name__)

_ACTIVE_TAB_DISABLES_RENAME_JS = ClientsideScript(Path(__file__).with_name("active_tab_disables_rename.js"))
_LIVE_SWITCH_STATE_JS = ClientsideScript(Path(__file__).with_name("live_switch_state.js"))
_LIVE_LAST_FETCH_LABEL_JS = ClientsideScript(Path(__file__).with_name("live_last_fetch_label.js"))
_PAGING_SIDE_EFFECTS_JS = ClientsideScript(Path(__file__).with_name("paging_side_effects.js"))

_SIZES_COOKIE_MAX_AGE: typing.Final = 365 * 24 * 60 * 60


# --- live updates: a `dcc.Interval`-driven poll of `Experiment.revision` re-renders whichever open
# charts have actually changed since the last poll; a small badge reports whether the poll itself
# is healthy. See `poll_for_updates` in `__init__.py`. ---
LIVE_POLL_INTERVAL_ID: IntervalId[ExperimentPage] = IntervalId("live-poll-interval")
STATE_LAST_KNOWN_REVISION: StoreId[ExperimentPage] = StoreId("last-known-revision")
LIVE_STATUS_ID: DivId[ExperimentPage] = DivId("live-status")

# --- enable/disable + "fetched Xs ago": both pure client-side concerns, deliberately kept off the
# server. `LIVE_UPDATES_ENABLED_ID`'s `checked` persists to the browser's own `localStorage` (Dash's
# built-in `persistence`/`persistence_type="local"`, set where it's rendered in
# `serve/_pages/experiment.py`) -- a per-browser viewing preference, not something to write into
# `Experiment`/`Page` and have every other viewer inherit. `LIVE_TICK_INTERVAL_ID` ticks
# `LIVE_LAST_FETCH_LABEL_ID`'s text once a second purely from `STATE_LAST_FETCH_AT` (a clientside
# callback, see `live_last_fetch_label.js`) -- no server round trip just to update a clock. ---
LIVE_UPDATES_ENABLED_ID: ValueId[ExperimentPage] = ValueId("live-updates-enabled")
LIVE_TICK_INTERVAL_ID: IntervalId[ExperimentPage] = IntervalId("live-tick-interval")
LIVE_LAST_FETCH_LABEL_ID: DivId[ExperimentPage] = DivId("live-last-fetch-label")
LIVE_PAUSED_BADGE_ID: DivId[ExperimentPage] = DivId("live-paused-badge")
"""The "Paused" badge shown instead of `LIVE_STATUS_ID` while live updates are switched off -- a
sibling element toggled by CSS `display` (see `live_switch_state.js`), not a second callback
writing `LIVE_STATUS_ID.children` -- `poll_for_updates` stays that output's only owner."""
STATE_LAST_FETCH_AT: StoreId[ExperimentPage] = StoreId("last-fetch-at")
"""ISO 8601 UTC timestamp of the last poll tick that actually reached the store successfully --
set on *every* such tick, whether or not anything had changed, since it answers "how stale could
what I'm looking at be" rather than "when did it last change"."""


class LiveUpdateSettings(BaseSettings):
    """`DLBOARD_*` environment variables controlling the experiment page's live-update poll."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    poll_interval_ms: int = 30000
    """
    How often the browser checks whether an open experiment has changed. Each check is one indexed
    primary-key read, cheap even with many concurrent viewers -- turn this down (or up) for a
    heavily-loaded multi-user deployment without a code change.
    """

    tick_interval_ms: int = 10000
    """How often the "fetched Xs ago" label re-renders. Purely client-side (see
    `LIVE_TICK_INTERVAL_ID`) -- this never touches the server, so it's fine to leave far more
    frequent than `poll_interval_ms`."""


def live_status_badge(*, ok: bool) -> Component:
    """
    Small colored chip reporting whether the live-update poll (`poll_for_updates`) is healthy.

    Stateless by design: a single failed poll is enough to flip it red, and it self-clears green on
    the very next successful one -- no failure-count/session state to track or leak, matching this
    feature's "don't trust clients, don't hold server-side connection state" approach.
    """
    return (
        dmc.Badge("Live", color="green", variant="dot", size="sm", tt="none")
        if ok
        else dmc.Badge("Reconnecting", color="red", variant="dot", size="sm", tt="none")
    )


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
        new_paging = clamped_paging(page, tab, requested)
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
        return (
            panel_area_children(panel_frames(store, page), page, tab, new_paging),
            new_paging.model_dump_json(),
            visible_open_panels(page, tab, new_paging),
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
        frames = panel_frames(store, page)
        changed = {
            name
            for name in requested.charts.keys() | paging.charts.keys()
            if requested.chart_paging(name) != paging.chart_paging(name) and name in panel_by_name
        }
        for name in changed:
            chart_paging = requested.chart_paging(name)
            number = visible_chart_indexes(panel_by_name[name], requested).number
            requested = requested.with_charts(name, chart_paging.model_copy(update={"page": number}))

        loaded = set(chart_ctx["loaded"] or [])
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
                if pid["panel"] in changed and pid["panel"] in loaded
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


def register_state_callbacks(app: Dash) -> None:
    """Callbacks belonging to the core render tree itself, not any one feature."""
    _register_paging(app)

    @app.callback(
        Output(STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(PANEL_ACCORDION.all(), "value"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(STATE_PAGE_STORAGE, "data"),
        State(PANEL_CONTENT.all(), "id"),
        prevent_initial_call=True,
    )
    def persist_open_panel(
        open_values: list[list[str] | None],
        experiment_id: int,
        page_json: str,
        shown_panels: list[dict[str, str]],
    ) -> str:
        curr_page = BasicExperimentPage.model_validate_json(page_json)
        # Only the panels on screen are in `open_values`; the open state of every other panel (on another
        # page or tab) is carried over untouched.
        shown = {pid["panel"] for pid in shown_panels}
        stored_open = curr_page.open_panels()
        open_value = [name for name in stored_open if name not in shown] + [
            name for group in open_values for name in (group or [])
        ]
        # Mirrors `toggle_panel_layout`'s own "a mount can echo the current value back as a
        # 'change'" check: the accordion's `value` prop mounting with whatever `accordion_view`
        # just initialized it to (every render, including ones that aren't about panels being open
        # at all) reports back here as if the user had just opened/closed something. Comparing
        # against what's already stored catches that mount echo before it turns into a write --
        # which matters beyond just the wasted round trip: unlike a real open/close, that write's
        # own `page_json` can be arbitrarily stale (captured whenever the accordion was last built,
        # not necessarily this instant), and writing it back can undo a *later* edit that branched
        # into a view of its own in between.
        if set(open_value) == set(stored_open):
            raise PreventUpdate
        store = get_data_store()
        # Which panel is expanded is per-viewer browsing state, not part of what the view "is" --
        # see `persist_settings`'s own docstring on why this one opts out of branching. Its ref
        # comes from the accordion's own page (`ref_of`), not `STATE_VIEW_ID` -- the accordion
        # remounting is itself often a *side effect* of a structural edit elsewhere on this same
        # page that just branched into a new view, and `STATE_VIEW_ID` only catches up to that a
        # separate round trip later. Reading it here would race that catch-up and, since this
        # write goes straight through regardless of ownership, land back on the view the edit had
        # already branched away from.
        page = persist_settings(
            store, ref_of(experiment_id, curr_page), {OPEN_PANEL_KEY: open_value}, branch_on_edit=False
        )
        return page.model_dump_json()

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
        # Same `ref_of`-over-`STATE_VIEW_ID` reasoning as `persist_open_panel` -- this accordion
        # remount can itself be the tail end of an edit that just branched into a new view.
        curr_page = BasicExperimentPage.model_validate_json(page_json)
        page = load_page(store, ref_of(experiment_id, curr_page))
        panel_by_name = {p.name: p for p in page.panels}
        frames = panel_frames(store, page)

        outputs: list[Any] = []
        for pid in panel_ids:
            name = pid["panel"]
            if name not in newly_opened or name not in panel_by_name:
                outputs.append(no_update)
                continue
            outputs.append(render_panel_content(frames, panel_by_name[name], paging))
        return outputs, sorted(loaded_set | newly_opened)

    @app.callback(
        Output(STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(PANEL_TABS_ID, "value"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def persist_active_tab(tab_value: str | None, experiment_id: int, page_json: str) -> str:
        if tab_value is None:
            raise PreventUpdate
        curr_page = BasicExperimentPage.model_validate_json(page_json)
        # Same mount-echo guard as `persist_open_panel`, and for the same reason: `PANEL_TABS_ID`
        # mounting with whichever tab `accordion_view` already had active reports back here as if
        # the user had just switched tabs.
        if curr_page.page_settings.get(ACTIVE_TAB_KEY) == tab_from_component_value(tab_value):
            raise PreventUpdate
        store = get_data_store()
        # Which tab is active is per-viewer browsing state -- same reasoning, including the ref,
        # as `persist_open_panel`.
        page = persist_settings(
            store,
            ref_of(experiment_id, curr_page),
            {ACTIVE_TAB_KEY: tab_from_component_value(tab_value)},
            branch_on_edit=False,
        )
        return page.model_dump_json()

    # Tab switching is entirely client-side (Mantine's own state, no server round trip) -- so
    # whether "Rename the active tab" should be disabled (on the ungrouped "General" tab) has to
    # update client-side too, not just from this render's initial `disabled=`.
    app.clientside_callback(
        _ACTIVE_TAB_DISABLES_RENAME_JS.source,
        Output(RENAME_TAB_BUTTON_ID, "disabled"),
        Input(PANEL_TABS_ID, "value"),
    )

    # Both pure client-side, deliberately never round-tripping to the server -- see
    # `live_switch_state.js`/`live_last_fetch_label.js` and the constants' own docstrings above.
    app.clientside_callback(
        _LIVE_SWITCH_STATE_JS.source,
        Output(LIVE_POLL_INTERVAL_ID, "disabled"),
        Output(LIVE_STATUS_ID, "style"),
        Output(LIVE_PAUSED_BADGE_ID, "style"),
        Input(LIVE_UPDATES_ENABLED_ID, "checked"),
    )
    app.clientside_callback(
        _LIVE_LAST_FETCH_LABEL_JS.source,
        Output(LIVE_LAST_FETCH_LABEL_ID, "children"),
        Input(LIVE_TICK_INTERVAL_ID, "n_intervals"),
        State(STATE_LAST_FETCH_AT, "data"),
    )
