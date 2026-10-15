"""
The panel area: the tab bar and toolbar, the active tab's page of panels, and every id they render.

Pure -- handed the page, the paging, and the `PanelFrames` already fetched for its open panels, never a store.
The modals and drawers its buttons open belong to the feature modules, which the experiment page renders once
beside it.
"""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING, Any, ClassVar, Final, cast

import dash_mantine_components as dmc
from dash import html
from dash.dcc import Store
from structlog.stdlib import get_logger

from dlboard import models
from dlboard.serve import Icon, icon
from dlboard.serve._component_ids import ButtonId, DivId, PatternId, StoreId, ValueId
from dlboard.serve._pages._dash_helpers import tooltipped_action_icon
from dlboard.serve._pages._experiment._page_model import (
    UNGROUPED_TAB,
    UNGROUPED_TAB_LABEL,
    BasicExperimentPage,
    group_panels_by_tab,
)
from dlboard.serve._pages._experiment._paging import (
    CHART_SIZES,
    PANEL_SIZES,
    Paging,
    Slice,
    reveal,
    visible_chart_indexes,
    visible_open_panels,
    visible_panels,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pandas as pd
    from dash.development.base_component import Component

    from dlboard.serve._pages._experiment._panel_data import PanelFrames

from dlboard.serve._pages._experiment._chart_view import (
    drag_handle,
    render_panel_charts,
)
from dlboard.serve._pages._experiment._ids import (
    PANEL_PAGING_ID,
    ExperimentPage,
)

_log = get_logger(__name__)


# --- tabs ---
PANEL_TABS_ID: ValueId[ExperimentPage] = ValueId("experiment-panel-tabs")

_UNGROUPED_TAB_VALUE: typing.Final = "ungrouped"
"""
`dmc.Tabs`/`TabsTab`/`TabsPanel`'s own `value` prop, for `UNGROUPED_TAB` specifically.

Mantine's `Tabs` treats an empty-string `value` as "no value was set" and refuses to render that
tab/panel at all (a real, silent failure: the tab becomes unclickable, so there's no way back to
the ungrouped panels once another tab exists) -- so component `value`s always go through
`tab_component_value`/`tab_from_component_value` instead of using `PanelInstance.tab`/
`Paging.tab`'s own `""` directly. A Private-Use-Area character prefix keeps this from
colliding with a tab name any user could actually type into the new-tab modal. Not underscored --
`_panel_controls.py`'s per-panel tab-move dropdown needs the same mapping for its own `Select`.
"""


def tab_component_value(tab: str) -> str:
    """Map a real tab name (`""` for the ungrouped tab) to what a Mantine component accepts as `value`."""
    return tab or _UNGROUPED_TAB_VALUE


def tab_from_component_value(value: str) -> str:
    """Invert `tab_component_value` -- what a component's `value`/`Input` reports back."""
    return "" if value == _UNGROUPED_TAB_VALUE else value


LOADED_PANELS_STORE_ID: StoreId[ExperimentPage] = StoreId("loaded-panels-store")

FIRST_PAGE: typing.Final = Paging()
"""Every panel list's first page, unfiltered -- what a render with no particular paging shows."""

PANEL_AREA_ID: DivId[ExperimentPage] = DivId("panel-area")
"""What a page flip re-renders: the panel pagers and the active tab's accordion, and nothing else."""
PANEL_FILTER_ID: ValueId[ExperimentPage] = ValueId("panel-filter")
PANEL_PAGER_ID: ValueId[ExperimentPage] = ValueId("panel-pager")
PANEL_PAGE_SIZE_ID: ValueId[ExperimentPage] = ValueId("panel-page-size")
CHART_PAGE_SIZE_ID: ValueId[ExperimentPage] = ValueId("chart-page-size")

# --- new-panel popover, anchored to the toolbar's "New panel" button (callback in `_panel_controls.py`) ---
NEW_PANEL_OPEN_ID: ButtonId[ExperimentPage] = ButtonId("new-panel-open")
NEW_PANEL_NAME_ID: ValueId[ExperimentPage] = ValueId("panel-name")
NEW_PANEL_ID: ButtonId[ExperimentPage] = ButtonId("new-panel-button")

# --- buttons in the panel area that open a modal or drawer -- each rendered once, beside the panel
# area, by the feature module that owns it (`_chart_suggestions.py`, `_tab_controls.py`). ---
SUGGEST_CHARTS_BUTTON_ID: ButtonId[ExperimentPage] = ButtonId("suggest-charts-button")
AUTO_POPULATE_OPEN_ID: ButtonId[ExperimentPage] = ButtonId("auto-populate-open")
NEW_TAB_BUTTON_ID: ButtonId[ExperimentPage] = ButtonId("new-tab-button")
RENAME_TAB_BUTTON_ID: ButtonId[ExperimentPage] = ButtonId("rename-tab-button")


class PanelPattern(PatternId[ExperimentPage]):
    """A family of components with one per panel, by its name."""

    KEYS: ClassVar[tuple[str, ...]] = ("panel",)

    def of(self, panel_name: str) -> dict[str, str]:
        return {"type": self.type, "panel": panel_name}


class TabPattern(PatternId[ExperimentPage]):
    """A family of components with one per tab, by its name (`""` for the ungrouped tab)."""

    KEYS: ClassVar[tuple[str, ...]] = ("tab",)

    def of(self, tab: str) -> dict[str, str]:
        return {"type": self.type, "tab": tab}


# --- the panel area's own per-tab, per-panel and per-chart components ---
PANEL_ACCORDION: Final = TabPattern("panel-accordion")
"""One `Accordion` per tab group -- see `_page_view`."""
PANEL_HEADER: Final = PanelPattern("panel-header")
PANEL_CONTENT: Final = PanelPattern("panel-content")
PANEL_DRAG_HANDLE: Final = PanelPattern("panel-drag-handle")
PANEL_SYNC_SWITCH: Final = PanelPattern("panel-sync")
PANEL_LAYOUT_CONTROL: Final = PanelPattern("panel-layout")
PANEL_GRID_COLUMNS: Final = PanelPattern("panel-grid-columns")
PANEL_GRID: Final = PanelPattern("panel-grid")
OPEN_CHART_BUTTON: Final = PanelPattern("open-add-chart")
RENAME_PANEL_BUTTON: Final = PanelPattern("rename-panel")
DELETE_PANEL_BUTTON: Final = PanelPattern("delete-panel")
PANEL_SUGGEST_BUTTON: Final = PanelPattern("panel-suggest-charts")
CHART_FILTER: Final = PanelPattern("chart-filter")
CHART_PAGER: Final = PanelPattern("chart-pager")
CHART_PAGER_SLOT: Final = PanelPattern("chart-pager-slot")


def render_panel_content_from_df(
    panel: models.PanelInstance[Any, Any],
    df: pd.DataFrame,
    indexes: Sequence[int] | None = None,
    page_size: int = models.DEFAULT_RENDER_CONTEXT.page_size,
) -> Component:
    """Render a panel's charts at `indexes` from an already-fetched dataframe -- see `render_panel_content`."""
    items = render_panel_charts(panel, df, indexes, page_size)
    # Read by the image series' slider lock (`image_series_lock.js`): which charts move together.
    sync_marker = cast("dict[str, Any]", {"data-dl-panel-sync": "1"} if panel.sync else {})
    return (
        dmc.SimpleGrid(
            items, id=PANEL_GRID.of(panel.name), cols=panel.grid_columns, spacing="lg", **sync_marker
        )
        if panel.layout == "grid"
        else dmc.Flex(items, justify="flex-start", gap="lg", wrap="wrap", **sync_marker)
    )


def render_panel_content(
    frames: PanelFrames, panel: models.PanelInstance[Any, Any], paging: Paging
) -> Component:
    """
    Render the page of one panel's charts `paging` shows, from `frames`. Only called for panels that are open.

    `frames` must hold the panel's data whenever `paging` shows a chart of it (see `panel_frames`).
    """
    indexes = visible_chart_indexes(panel, paging).items
    if not indexes:
        return dmc.Text(
            f"No charts match {paging.chart_paging(panel.name).q!r}",
            c="dimmed",
            size="sm",
            ta="center",
            py="md",
        )
    return render_panel_content_from_df(panel, frames[panel.name], indexes, paging.sizes.charts)


def _panel_placeholder() -> dmc.Skeleton:
    return dmc.Skeleton(height=60, radius="sm")


def panel_header_controls(panel: models.PanelInstance[Any, Any]) -> Component:
    """
    Drag, add and an actions menu for one panel, hover-revealed on the panel's own header.

    The menu holds everything used less often (suggest, rename, sync, layout, delete): Mantine
    mounts a menu's dropdown only once it opens, so a page of hundreds of panels pays for one
    icon button apiece instead of ~30 components. The callbacks over those controls are
    pattern-matched (`ALL`), so they simply see the controls appear when the menu opens.

    Moving a panel to a tab is drag-and-drop (drag the same handle used to reorder panels onto a
    tab in the tab bar, see `_experiment_page_dragdrop.js`), not a control here.
    """
    panel_name = panel.name
    return dmc.Group(
        [
            drag_handle(
                class_name="dl-panel-drag-handle",
                component_id=PANEL_DRAG_HANDLE.of(panel_name),
                data_attrs={"panel-name": panel_name},
            ),
            html.Span(
                dmc.ActionIcon(
                    icon(Icon.ADD),
                    id=OPEN_CHART_BUTTON.of(panel_name),
                    n_clicks=0,
                    variant="subtle",
                    size="sm",
                    color="gray",
                    **cast("dict[str, Any]", {"aria-label": "Add chart to this panel"}),
                ),
                title="Add chart to this panel",
            ),
            dmc.Menu(
                [
                    dmc.MenuTarget(
                        dmc.ActionIcon(
                            # Not the three dots: next to the drag handle's grip (also dots) that
                            # read as one more handle, not as a menu.
                            icon(Icon.OPTIONS),
                            variant="subtle",
                            size="sm",
                            color="gray",
                            **cast("dict[str, Any]", {"aria-label": "Panel actions"}),
                        )
                    ),
                    dmc.MenuDropdown(
                        [
                            dmc.MenuItem(
                                "Suggest charts",
                                id=PANEL_SUGGEST_BUTTON.of(panel_name),
                                leftSection=icon(Icon.SUGGEST),
                            ),
                            dmc.MenuItem(
                                "Rename panel…",
                                id=RENAME_PANEL_BUTTON.of(panel_name),
                                leftSection=icon(Icon.EDIT),
                            ),
                            dmc.MenuDivider(),
                            dmc.Switch(
                                id=PANEL_SYNC_SWITCH.of(panel_name),
                                label="Sync crosshair and step across charts",
                                checked=panel.sync,
                                size="xs",
                                px="sm",
                                py="xs",
                            ),
                            dmc.SegmentedControl(
                                id=PANEL_LAYOUT_CONTROL.of(panel_name),
                                data=[
                                    {"value": "packed", "label": "Packed"},
                                    {"value": "grid", "label": "Grid"},
                                ],
                                value=panel.layout,
                                size="xs",
                                fullWidth=True,
                            ),
                            *(
                                [
                                    dmc.NumberInput(
                                        id=PANEL_GRID_COLUMNS.of(panel_name),
                                        value=panel.grid_columns,
                                        min=models.MIN_GRID_COLUMNS,
                                        max=models.MAX_GRID_COLUMNS,
                                        allowDecimal=False,
                                        allowNegative=False,
                                        clampBehavior="strict",
                                        size="xs",
                                        mt="xs",
                                        **cast("dict[str, Any]", {"aria-label": "Grid columns"}),
                                    )
                                ]
                                if panel.layout == "grid"
                                else []
                            ),
                            dmc.MenuDivider(),
                            dmc.MenuItem(
                                "Delete panel…",
                                id=DELETE_PANEL_BUTTON.of(panel_name),
                                leftSection=icon(Icon.DELETE),
                                color="red",
                            ),
                        ],
                        w=240,
                    ),
                ],
                position="bottom-end",
            ),
        ],
        className="dl-panel-controls",
        gap="xs",
        wrap="nowrap",
        # The header runs to the panel's border; without this the menu sat right against it.
        pr="sm",
    )


def _chart_filter_offered(panel: models.PanelInstance[Any, Any], chart_query: str) -> bool:
    """Whether a panel gets a chart filter: it has more charts than the smallest page, or one is on."""
    return len(panel.charts) > CHART_SIZES[0] or bool(chart_query)


def _chart_filter(panel: models.PanelInstance[Any, Any], chart_query: str) -> dmc.TextInput:
    """Fuzzy filter on the metric and artifact names a panel's charts show (see `chart_search_keys`)."""
    return dmc.TextInput(
        id=CHART_FILTER.of(panel.name),
        value=chart_query,
        placeholder="Filter charts",
        leftSection=icon(Icon.SEARCH),
        debounce=300,
        size="xs",
        w=200,
    )


def panel_header(panel: models.PanelInstance[Any, Any], chart_query: str = "") -> Component:
    """
    A panel's accordion header.

    `AccordionControl`, a chart filter box (when there are enough charts to page) and hover-revealed
    controls as true DOM siblings (all direct children of this wrapping `Group`) -- not nested inside
    the control's own `<button>`, which native HTML doesn't allow another interactive element inside
    anyway. The filter box sits in the header, not above the charts, so it costs no vertical space; it
    isn't one of the hover-revealed controls, so a filter that is on stays in sight.
    """
    n_charts = len(panel.charts)
    count_label = f"{n_charts} chart" if n_charts == 1 else f"{n_charts} charts"
    return dmc.Group(
        [
            dmc.AccordionControl(
                dmc.Group(
                    [
                        dmc.Text(panel.name, size="xs", fw=600),
                        # How much is inside, before opening it -- a big panel can take a while to render.
                        *(
                            [dmc.Badge(count_label, variant="light", size="xs", tt="none")]
                            if n_charts
                            else []
                        ),
                    ],
                    gap="xs",
                    wrap="nowrap",
                ),
                py="xs",
                px="xs",
                style={"flex": 1},
                # The badge stays out of the button's name (tests and screen readers look a panel up by it);
                # the count is its description instead.
                **cast("dict[str, Any]", {"aria-label": panel.name, "aria-description": count_label}),
            ),
            *([_chart_filter(panel, chart_query)] if _chart_filter_offered(panel, chart_query) else []),
            panel_header_controls(panel),
        ],
        id=PANEL_HEADER.of(panel.name),
        className="dl-panel-item-header",
        gap=0,
        wrap="nowrap",
    )


def _page_view(frames: PanelFrames, page: BasicExperimentPage, paging: Paging) -> Component:
    """
    Render the toolbar and the active tab's page of panels. Only open panels get real content.

    Only the active tab's panels are rendered, and of those only the page `paging` is on (see
    `panel_area`) -- a tab or an experiment with hundreds of panels costs the same as one with a
    few. If every panel shares the (default) ungrouped tab -- the common case today -- no `Tabs`
    chrome is shown at all, so this looks exactly like a plain accordion until a panel actually
    gets tabbed. Above the panels sits one toolbar row: tabs on the left, followed by the labeled
    "New tab" button, the *only* place a brand-new tab gets created (see `NEW_TAB_BUTTON_ID`) --
    moving a panel to one that already exists is dragging its drag handle onto that tab
    (`.dl-tab-target`, see `_experiment_page_dragdrop.js`), the same handle used to reorder
    panels. The panel filter and panel actions sit on the right. A view with no panels at all
    shows an empty state offering to auto-generate them.
    """
    new_tab_button = dmc.Button(
        "New tab",
        id=NEW_TAB_BUTTON_ID,
        leftSection=icon(Icon.ADD),
        variant="subtle",
        color="gray",
        size="xs",
    )
    if not page.panels:
        return html.Div([_toolbar([new_tab_button], [_new_panel_popover()]), _empty_view()])

    tabs = list(group_panels_by_tab(page.panels))
    active_tab = paging.shown_tab(page.panels)
    area = panel_area(frames, page, active_tab, paging)
    panel_actions: list[Component] = [
        _panel_filter(paging),
        _new_panel_popover(),
        _suggest_charts_button(),
    ]
    if len(tabs) <= 1:
        return html.Div([_toolbar([new_tab_button], panel_actions), area])

    rename_tab_button = tooltipped_action_icon(
        Icon.EDIT,
        component_id=RENAME_TAB_BUTTON_ID,
        label="Rename the active tab",
        size="xs",
        disabled=active_tab == UNGROUPED_TAB,
    )
    return dmc.Tabs(
        id=PANEL_TABS_ID,
        value=tab_component_value(active_tab),
        children=[
            _toolbar(
                [dmc.TabsList([_tab_tab(tab) for tab in tabs]), rename_tab_button, new_tab_button],
                panel_actions,
            ),
            area,
        ],
    )


def _panel_footer(shown: Slice[Any], size_controls: list[Component]) -> Component:
    """
    The row under the panels: the page selector centered, and the page-size choices at its right.

    The selector has `withEdges` for first/last, and nothing is drawn when there is one page and no
    size choice to offer. The two share a row, below what they page, so neither takes room above it.
    """
    if shown.total <= 1 and not size_controls:
        return html.Div()
    return dmc.Flex(
        [
            html.Div(style={"flex": 1}),
            dmc.Pagination(
                id=PANEL_PAGER_ID, total=shown.total, value=shown.number, withEdges=True, size="sm"
            )
            if shown.total > 1
            else html.Div(),
            html.Div(
                dmc.Group(size_controls, gap="sm", wrap="nowrap"),
                style={"flex": 1, "display": "flex", "justifyContent": "flex-end"},
            ),
        ],
        align="center",
        my="xs",
    )


def _page_size_controls(page: BasicExperimentPage, paging: Paging) -> list[Component]:
    """
    The panels-per-page and charts-per-page choices, each offered once something is big enough to page.

    Judged over the whole page, not the active tab, so they don't come and go as you switch tabs.
    """
    return [
        dmc.Group(
            [
                dmc.Select(
                    id=select_id,
                    data=[{"value": str(size), "label": str(size)} for size in sizes],
                    value=str(current),
                    allowDeselect=False,
                    size="xs",
                    w=64,
                ),
                dmc.Text(label, size="xs", c="dimmed"),
            ],
            gap=4,
            wrap="nowrap",
        )
        for select_id, label, sizes, current, offered in (
            (
                PANEL_PAGE_SIZE_ID,
                "panels/page",
                PANEL_SIZES,
                paging.sizes.panels,
                len(page.panels) > PANEL_SIZES[0],
            ),
            (
                CHART_PAGE_SIZE_ID,
                "charts/page",
                CHART_SIZES,
                paging.sizes.charts,
                any(len(p.charts) > CHART_SIZES[0] for p in page.panels),
            ),
        )
        if offered
    ]


def _panel_filter(paging: Paging) -> dmc.TextInput:
    """Fuzzy filter on the panel names of the active tab. Lives in the toolbar, outside `panel_area`, so typing never remounts it."""
    return dmc.TextInput(
        id=PANEL_FILTER_ID,
        value=paging.panel_q,
        placeholder="Filter panels",
        leftSection=icon(Icon.SEARCH),
        debounce=300,
        size="xs",
        w=200,
    )


def panel_area_children(
    frames: PanelFrames, page: BasicExperimentPage, tab: str, paging: Paging
) -> list[Component]:
    """The accordion and footer for the page of `tab`'s panels `paging` shows -- all that a page flip re-renders."""
    shown = visible_panels(page.panels, tab, paging)
    accordion = _panel_accordion(frames, tab, shown.items, set(paging.opened(page.panels)), paging)
    no_match = (
        dmc.Text(f"No panels match {paging.panel_q!r}", c="dimmed", size="sm", ta="center", py="xl")
        if paging.panel_q and not shown.items
        else html.Div()
    )
    return [no_match, accordion, _panel_footer(shown, _page_size_controls(page, paging))]


def panel_area(frames: PanelFrames, page: BasicExperimentPage, tab: str, paging: Paging) -> html.Div:
    return html.Div(panel_area_children(frames, page, tab, paging), id=PANEL_AREA_ID)


def clamped_paging(page: BasicExperimentPage, tab: str, paging: Paging) -> Paging:
    """`paging` with any page that no longer exists (panels or charts deleted, a stale link) moved to the last one."""
    paging = paging.with_panels(page=visible_panels(page.panels, tab, paging).number)
    for panel in visible_panels(page.panels, tab, paging).items:
        chart_paging = paging.chart_paging(panel.name)
        number = visible_chart_indexes(panel, paging).number
        if number != chart_paging.page:
            paging = paging.with_charts(panel.name, chart_paging.model_copy(update={"page": number}))
    return paging


def _toolbar(tab_controls: list[Component], panel_actions: list[Component]) -> dmc.Group:
    """
    The row above the panels: tab controls on the left, panel actions on the right.

    Part of the same full-width tree as the panels rather than a container off to the side, which
    squeezed the panels into sharing a row with it.
    """
    return dmc.Group(
        [dmc.Group(tab_controls, gap="xs", wrap="nowrap"), dmc.Group(panel_actions, gap="xs", wrap="nowrap")],
        justify="space-between",
        wrap="nowrap",
        gap="xs",
        mb="sm",
    )


def _tab_tab(tab: str) -> Component:
    """One `TabsTab`, also a drag-and-drop target for moving a panel to this (existing) tab."""
    # `data-*` attrs aren't in `TabsTab`'s typed signature -- `cast` to `Any` rather than fight that.
    tabs_tab = cast("Any", dmc.TabsTab)
    return tabs_tab(
        tab or UNGROUPED_TAB_LABEL,
        value=tab_component_value(tab),
        className="dl-tab-target",
        **{"data-tab": tab},
    )


def chart_pager(panel: models.PanelInstance[Any, Any], shown: Slice[int]) -> Component:
    """A panel's centered chart pager, under its charts; nothing when there is one page."""
    if shown.total <= 1:
        return html.Div()
    return dmc.Group(
        dmc.Pagination(
            id=CHART_PAGER.of(panel.name), total=shown.total, value=shown.number, withEdges=True, size="xs"
        ),
        justify="center",
        mt="sm",
    )


def chart_pager_slot(panel: models.PanelInstance[Any, Any], paging: Paging) -> html.Div:
    """
    The pager under a panel's charts, in a slot of its own so a filter change can redraw it.

    A sibling of `PANEL_CONTENT`'s div, not inside it: `poll_for_updates` patches into that one.
    """
    return html.Div(
        chart_pager(panel, visible_chart_indexes(panel, paging)), id=CHART_PAGER_SLOT.of(panel.name)
    )


def _panel_accordion(
    frames: PanelFrames,
    tab: str,
    panels: list[models.PanelInstance[Any, Any]],
    open_set: set[str],
    paging: Paging,
) -> dmc.Accordion:
    return dmc.Accordion(
        id=PANEL_ACCORDION.of(tab),
        multiple=True,
        value=[p.name for p in panels if p.name in open_set],
        variant="separated",
        className="dl-panels",
        chevronPosition="left",
        children=[
            dmc.AccordionItem(
                [
                    panel_header(p, paging.chart_paging(p.name).q),
                    dmc.AccordionPanel(
                        [
                            # `data-panel-name` isn't in `html.Div`'s typed signature -- `cast` to `Any`
                            # rather than fight that (same as `drag_handle`'s data attrs). Read by
                            # `_experiment_page_dragdrop.js` to let a dragged chart be dropped anywhere
                            # in a *different* panel's body, not only onto one of its existing charts.
                            #
                            # This div's own children must stay exactly `render_panel_content(...)`'s
                            # output (or the placeholder) -- `poll_for_updates` (`_experiment/__init__.py`)
                            # patches live-update data straight into each individual chart's own
                            # `CHART_CONTENT.of(...)` node inside here, bypassing this whole
                            # `_panel_accordion` tree. Any chrome added here later (e.g. a per-panel
                            # "last updated" badge) that isn't also part of `render_panel_content`'s
                            # output would vanish the next time a live-update poll patches an open panel.
                            # That's why the chart filter and pager above are siblings of it, not in it.
                            cast("Any", html.Div)(
                                id=PANEL_CONTENT.of(p.name),
                                className="dl-panel-body",
                                **{"data-panel-name": p.name},
                                children=(
                                    render_panel_content(frames, p, paging)
                                    if p.name in open_set
                                    else _panel_placeholder()
                                ),
                            ),
                            chart_pager_slot(p, paging),
                        ],
                        px="xs",
                        py="xs",
                    ),
                ],
                p.name,
            )
            for p in panels
        ],
    )


def _new_panel_popover() -> dmc.Popover:
    """
    The toolbar's "New panel" button, and the name form it opens right beneath it.

    Creating the panel re-renders the whole panel area, this popover included, which is what
    closes it -- no open/close state to keep. A bad name leaves it open with the error inline.
    """
    return dmc.Popover(
        [
            dmc.PopoverTarget(
                dmc.Button(
                    "New panel", id=NEW_PANEL_OPEN_ID, leftSection=icon(Icon.ADD), variant="light", size="xs"
                )
            ),
            dmc.PopoverDropdown(
                dmc.Stack(
                    [
                        dmc.TextInput(
                            id=NEW_PANEL_NAME_ID,
                            label="Panel name",
                            placeholder="e.g. Losses",
                            size="xs",
                            **cast("dict[str, Any]", {"data-autofocus": True}),
                        ),
                        dmc.Group(
                            dmc.Button("Create panel", id=NEW_PANEL_ID, n_clicks=0, size="xs"),
                            justify="flex-end",
                        ),
                    ],
                    gap="xs",
                )
            ),
        ],
        position="bottom-end",
        width=260,
        withArrow=True,
        shadow="md",
        trapFocus=True,
    )


def _suggest_charts_button() -> dmc.Button:
    return dmc.Button(
        "Suggest charts",
        id=SUGGEST_CHARTS_BUTTON_ID,
        n_clicks=0,
        leftSection=icon(Icon.SUGGEST),
        variant="subtle",
        size="xs",
    )


def _empty_view() -> Component:
    """A view with no panels: offer to auto-generate them (behind `_chart_suggestions.py`'s modal, which previews the result first)."""
    return html.Div(
        [
            dmc.Paper(
                dmc.Stack(
                    [
                        dmc.ThemeIcon(icon(Icon.SUGGEST), size="xl", radius="xl", variant="light"),
                        dmc.Text("No charts in this view yet", fw=600, size="lg"),
                        dmc.Text(
                            "Auto-generate a panel for each group of logged metrics and artifacts, "
                            "or start from an empty panel with New panel.",
                            c="dimmed",
                            size="sm",
                            ta="center",
                            maw=440,
                        ),
                        dmc.Button(
                            "Auto-generate charts",
                            id=AUTO_POPULATE_OPEN_ID,
                            n_clicks=0,
                            leftSection=icon(Icon.SUGGEST),
                            variant="gradient",
                            mt="xs",
                        ),
                    ],
                    align="center",
                    gap="xs",
                ),
                withBorder=True,
                radius="md",
                p="xl",
                style={"borderStyle": "dashed"},
            ),
        ]
    )


def _focus_on_chart(page: BasicExperimentPage, paging: Paging, chart_id: str) -> Paging:
    """
    `paging` as rendered for a `?chart=` deep link: that chart's tab shown, its panel open and on screen.

    Only `Paging` changes -- the URL, never the page -- so following a link someone shared changes
    nothing for anyone else. An id that matches no chart leaves `paging` as it is.
    """
    panel = next((p for p in page.panels if any(c.id == chart_id for c in p.charts)), None)
    if panel is None:
        return paging
    focused = paging.viewing(tab=panel.tab, open_panels=[*paging.opened(page.panels), panel.name])
    return reveal(page.panels, focused, panel_name=panel.name, chart_id=chart_id)


def resolved_paging(page: BasicExperimentPage, paging: Paging, *, focus_chart: str | None = None) -> Paging:
    """
    The `paging` `accordion_view` renders `page` with, which is also what its `PanelFrames` must be fetched for.

    `focus_chart` (a `ChartInstance.id`, from a `?chart=` deep link) opens that chart's panel and
    tab -- see `_focus_on_chart`. Any page that no longer exists is moved onto the last one that does, and the
    defaults are left out (`Paging.normalized`).
    """
    if focus_chart:
        paging = _focus_on_chart(page, paging, focus_chart)
    return clamped_paging(page, paging.shown_tab(page.panels), paging).normalized(page.panels)


def accordion_view(frames: PanelFrames, page: BasicExperimentPage, paging: Paging) -> html.Div:
    """
    The full accordion/tabs view of `page` (the modals it opens belong to their features, rendered once).

    Takes the page itself rather than re-reading it: every caller already has the version it just
    loaded or saved, which is also the only way to be sure a view is rendered, not the shared page.

    `paging` is which tab, open panels and page of panels to show, as `resolved_paging` left it, and `frames`
    the data of the open panels on it (`panel_frames`). The store this emits (`PANEL_PAGING_ID`) holds that
    paging.
    """
    _log.debug("rendering chart for experiment %s", page.experiment_id)
    tab = paging.shown_tab(page.panels)

    return html.Div(
        [
            _page_view(frames, page, paging),
            Store(id=LOADED_PANELS_STORE_ID, data=visible_open_panels(page.panels, tab, paging)),
            Store(id=PANEL_PAGING_ID, data=paging.model_dump_json()),
        ],
    )
