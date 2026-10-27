"""One chart in a panel: its body, the hover controls above it, and the ids it renders. Pure -- handed its data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Final, TypedDict, cast

import dash_mantine_components as dmc
from dash import html
from structlog.stdlib import get_logger

from dlboard import models
from dlboard.serve import Icon, icon
from dlboard.serve._component_ids import PatternId
from dlboard.serve._pages._dash_helpers import tooltipped_action_icon

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import pandas as pd
    from dash.development.base_component import Component

from dlboard.serve._pages._experiment._ids import CHART_VIEW_SAVE_REQUEST_ID, ExperimentPage
from dlboard.serve._pages._experiment._panel_data import ChartFrames, WideHistory, chart_data_fingerprint

_log = get_logger(__name__)


class ChartTargetData(TypedDict):
    panel: str
    index: int | None


class ChartID(ChartTargetData):
    type: str


class ChartPattern(PatternId[ExperimentPage]):
    """A family of components with one per chart, by its panel's name and its index in that panel."""

    KEYS: ClassVar[tuple[str, ...]] = ("panel", "index")

    def of(self, panel_name: str, index: int) -> dict[str, str | int]:
        return {"type": self.type, "panel": panel_name, "index": index}


CHART_CONTENT: Final = ChartPattern("chart-content")
CHART_CONTROLS_GROUP: Final = ChartPattern("chart-controls")
CHART_DRAG_HANDLE: Final = ChartPattern("chart-drag-handle")
EDIT_CHART_BUTTON: Final = ChartPattern("edit-chart")
COPY_CHART_LINK_BUTTON: Final = ChartPattern("copy-chart-link")
DELETE_CHART_BUTTON: Final = ChartPattern("delete-chart")
CHART_BODY: Final = ChartPattern("chart-body")
"""A chart item's body: the chart itself, what a live update replaces."""
CHART_FINGERPRINT: Final = ChartPattern("chart-fingerprint")
"""The hidden node inside a `CHART_BODY` that carries the `chart_data_fingerprint` of the data it was drawn from."""
CHART_DRAFT: Final = ChartPattern("chart-draft")
"""
Where the chart editor draws the chart being edited, in its own card: while it holds something, the card shows it in
place of its `CHART_BODY` (`_experiment_page_hover.css`). The body is left alone under it, so a live update still has
a chart to replace and cancelling the edit is just emptying this.
"""
DRAFT_SUFFIX: Final = "~draft"
"""Added to a draft's chart id for the ids it renders under: the chart it replaces is still in the page, and two charts cannot share Dash ids."""


def _render_chart_safely(
    chart: models.ChartInstance[Any, Any],
    dataframe: pd.DataFrame,
    panel_name: str,
    context: models.RenderContext,
) -> Component:
    """Render one chart, isolating failures so a broken chart doesn't take the rest of the panel down."""
    try:
        return chart.render(dataframe, context)
    except Exception as exc:  # noqa: BLE001
        _log.exception("Failed to render %r chart in panel %r", chart.chart_type, panel_name)
        return dmc.Alert(
            f"{type(exc).__name__}: {exc}",
            title=f"Failed to render {chart.chart_type} chart",
            color="red",
            variant="light",
        )


def _apply_panel_sync(rendered: Component, *, panel_name: str, sync: bool) -> Component:
    """
    Scope a rendered line chart's crosshair sync to its own panel, or drop it if sync is off.

    A chart's own `syncId` is just its x-axis column name (e.g. "step"), shared by every line
    chart on the page with that x-axis -- not just the ones in the same panel. Two panels that
    both use "step" (e.g. train vs. val, whose step ranges don't correspond to each other) would
    otherwise wrongly sync their tooltips together.
    """
    # A line chart sits under its own title, so it is found anywhere in what the chart rendered.
    for node in (rendered, *rendered._traverse()):  # noqa: SLF001
        if not isinstance(node, dmc.LineChart):
            continue
        untyped = cast("Any", node)
        props: dict[str, Any] = dict(untyped.lineChartProps or {})
        sync_id = props.pop("syncId", None)
        if sync and sync_id is not None:
            props["syncId"] = f"{panel_name}:{sync_id}"
        untyped.lineChartProps = props
    return rendered


_NARROWER: Final = (
    "show their last value (a table of runs, a bar chart) or their spread (a series band) instead"
)
"""What to do about a chart over too many metrics' every step."""


def _read_badge(chart: models.ChartInstance[Any, Any], wide: WideHistory | None) -> Component | None:
    """
    A small, always-visible warning badge for a chart whose read grows with the experiment rather than with what it shows.

    A chart that draws every metric (e.g. a table chart with no `metrics` filter, left at its "show every metric"
    default) and draws from every step (its request's `reductions`) means fetching every step of every metric logged
    for the whole experiment, every time its panel updates; so does one over a pattern that matches hundreds (`wide`).
    That's a real, sometimes-significant cost the user has no other way to notice (it only ever shows up as a backend
    debug log), so it gets a badge instead of hover-only controls: this should be visible without having to go looking
    for it.
    """
    request = chart.data_request()
    if request.every_metric and request.reductions.default.reads_every_step:
        text = "fetches all metrics"
        label = (
            "No metrics filter is set, so this chart fetches every metric in the experiment on every update -- can be "
            'slow on a large experiment. Set specific metrics in "Edit chart" to scope it down.'
        )
    elif wide is not None and not wide.refused:
        text = f"reads {wide.metrics:,} metrics' history"
        label = f"This chart draws every step of {wide.metrics:,} metrics, on every update. To see that many, {_NARROWER}."
    else:
        return None
    return dmc.Tooltip(
        dmc.Badge(text, color="yellow", variant="light", size="xs", radius="sm"),
        label=label,
        position="top",
        withArrow=True,
        multiline=True,
        w=260,
    )


def _refused(wide: WideHistory) -> Component:
    """What a chart over too many metrics' every step shows in its place: why, and what to draw instead."""
    return dmc.Alert(
        f"This chart would draw every step of {wide.metrics:,} metrics, more than one chart can show, so their "
        f'history was not read. In "Edit chart", {_NARROWER}.',
        title="Too many metrics to draw whole",
        color="yellow",
        variant="light",
    )


def chart_body(
    panel: models.PanelInstance[Any, Any],
    index: int,
    dataframe: pd.DataFrame,
    page_size: int,
    *,
    wide: WideHistory | None,
) -> list[Component]:
    """
    What a chart item shows below its controls: the chart, and a hidden node holding the fingerprint it is drawn from.

    The fingerprint travels with the DOM it describes (not in a store the page keeps beside it), so a
    chart mounted any way at all -- first load, a panel opening, a page of charts, an edit -- says what
    it was drawn from, and the poll redraws it only when that is no longer what the data says. A chart over
    too many metrics' every step (`wide`, refused) shows why it is not drawn instead.
    """
    chart = panel.charts[index]
    fingerprint = html.Div(
        chart_data_fingerprint(dataframe),
        id=CHART_FINGERPRINT.of(panel.name, index),
        hidden=True,
    )
    drawn = (
        _refused(wide)
        if wide is not None and wide.refused
        else _draw_chart(panel, chart, chart.id, dataframe, page_size)
    )
    return [drawn, fingerprint]


def draft_view(
    panel: models.PanelInstance[Any, Any],
    draft: models.ChartInstance[Any, Any],
    dataframe: pd.DataFrame,
    page_size: int,
) -> Component:
    """
    A chart being edited, drawn as it would be in `panel`: what the chart editor puts in a card's `CHART_DRAFT`.

    Under ids of its own (`DRAFT_SUFFIX`), and with no fingerprint: it is not a chart of the page, so a live update never
    looks at it, and the chart it stands in for keeps its ids and keeps updating beneath it.
    """
    return _draw_chart(panel, draft, f"{draft.id}{DRAFT_SUFFIX}", dataframe, page_size)


def _draw_chart(
    panel: models.PanelInstance[Any, Any],
    chart: models.ChartInstance[Any, Any],
    chart_id: str,
    dataframe: pd.DataFrame,
    page_size: int,
) -> Component:
    """`chart`, rendered for `panel`'s layout under the ids that `chart_id` names, and with the panel's sync applied."""
    context = models.RenderContext(
        columns=panel.grid_columns,
        page_size=page_size,
        chart_id=chart_id,
        viewer_state_key=chart_id if models.ChartTypeRegistry.keeps_viewer_state(chart) else "",
    )
    return _apply_panel_sync(
        _render_chart_safely(chart, dataframe, panel.name, context), panel_name=panel.name, sync=panel.sync
    )


def _view_bar() -> Component:
    """
    What a card of a chart that keeps viewer state shows while the viewer has changed it (`chart_view_state.js`).

    The browser shows and handles it: "Save as default" asks `CHART_VIEW_SAVE_REQUEST_ID` to make the change the
    chart's own settings, and "Reset" drops it.
    """
    div = cast("Any", html.Div)
    button = cast("Any", html.Button)
    return div(
        [
            html.Span("View changed"),
            button(
                "Save as default",
                type="button",
                className="dl-chart-view-save",
                title="Make this the chart's own settings, for everyone who opens the page",
            ),
            button(
                "Reset", type="button", className="dl-chart-view-reset", title="Go back to the saved chart"
            ),
        ],
        className="dl-chart-view-bar",
        **{"data-dl-save-store": CHART_VIEW_SAVE_REQUEST_ID},
    )


def render_chart_item(
    panel: models.PanelInstance[Any, Any],
    index: int,
    frames: ChartFrames,
    page_size: int = models.DEFAULT_RENDER_CONTEXT.page_size,
) -> Component:
    """
    Render one chart in a panel (edit/delete controls above it, revealed on hover) as its own node.

    Individually addressable via `CHART_CONTENT.of(panel.name, index)`, and its chart (the body, below the
    controls) via `CHART_BODY`: a live-update poll (`poll_for_updates` in `__init__.py`) replaces
    exactly the body of a chart whose own data changed, instead of rebuilding `render_panel_charts`' whole
    blob for the panel: two charts sharing a panel (a common "Auto-generate charts"
    grouping) commonly have very different update cadences -- an epoch-level validation chart next to a
    per-step training chart -- and rebuilding the *panel* whenever *either* changes would pop the
    untouched one too.
    """
    chart = panel.charts[index]
    dataframe, wide = frames.for_chart(chart), frames.wide_history(chart)
    context = models.RenderContext(columns=panel.grid_columns, page_size=page_size, chart_id=chart.id)
    hover_controls = dmc.Group(
        [
            drag_handle(
                class_name="dl-chart-drag-handle",
                component_id=CHART_DRAG_HANDLE.of(panel.name, index),
                data_attrs={"panel-name": panel.name, "chart-index": str(index)},
            ),
            tooltipped_action_icon(
                Icon.EDIT,
                component_id=EDIT_CHART_BUTTON.of(panel.name, index),
                label="Edit chart",
                size="xs",
            ),
            # Copied client-side by `chart_deep_link.js`, from the page's own URL -- no round trip.
            tooltipped_action_icon(
                Icon.LINK,
                component_id=COPY_CHART_LINK_BUTTON.of(panel.name, index),
                label="Copy a link to this chart",
                size="xs",
                **cast("dict[str, Any]", {"data-dl-copy-chart-link": chart.id}),
            ),
            tooltipped_action_icon(
                Icon.DELETE,
                component_id=DELETE_CHART_BUTTON.of(panel.name, index),
                label="Delete chart",
                color="red",
                size="xs",
            ),
        ],
        id=CHART_CONTROLS_GROUP.of(panel.name, index),
        gap="xs",
        className="dl-chart-controls",
        # Laid over the chart's top-right corner rather than given a row of their own: a chart's
        # height is precious, and these only show on hover anyway.
        pos="absolute",
        top=4,
        right=4,
        bg="var(--mantine-color-body)",
        bdrs="sm",
        # Above a table's header, which the grid stacks over anything at 1 -- or Edit can't be clicked.
        style={"zIndex": 10},
    )
    # The badge (when present) must be a *sibling* of `hover_controls`, not nested inside it --
    # `dl-chart-controls`'s opacity:0-until-hover applies to its whole subtree, and this badge
    # is the one thing here that's supposed to stay visible without hovering.
    badge = _read_badge(chart, wide)
    keeps_view = models.ChartTypeRegistry.keeps_viewer_state(chart)
    return dmc.Stack(
        [
            *([dmc.Group([badge])] if badge else []),
            hover_controls,
            *([_view_bar()] if keeps_view else []),
            html.Div(
                chart_body(panel, index, dataframe, page_size, wide=wide),
                id=CHART_BODY.of(panel.name, index),
                className="dl-chart-body",
            ),
            html.Div(id=CHART_DRAFT.of(panel.name, index), className="dl-chart-draft"),
        ],
        id=CHART_CONTENT.of(panel.name, index),
        gap="xs",
        w="100%" if panel.layout == "grid" else chart.natural_width(context),
        maw="100%",
        p="xs",
        pos="relative",
        className="dl-chart-item",
        **cast(
            "dict[str, Any]",
            {"data-chart-id": chart.id, **({"data-dl-view-key": chart.id} if keeps_view else {})},
        ),
    )


def render_panel_charts(
    panel: models.PanelInstance[Any, Any],
    frames: ChartFrames,
    indexes: Sequence[int] | None = None,
    page_size: int = models.DEFAULT_RENDER_CONTEXT.page_size,
) -> list[Component]:
    """Render the charts of a panel at `indexes` (every chart, if not given), each from its own frame -- see `render_chart_item`."""
    return [
        render_chart_item(panel, idx, frames, page_size)
        for idx in range(len(panel.charts))
        if indexes is None or idx in indexes
    ]


def drag_handle(
    *, class_name: str, component_id: Mapping[str, str | int], data_attrs: dict[str, str]
) -> Component:
    """
    A grip glyph, draggable via `_experiment_page_dragdrop.js`.

    Its hint is a native `title`, not a `dmc.Tooltip`: every chart and panel carries one of these,
    and a mounted Mantine tooltip costs the browser several milliseconds apiece.
    """
    # `data-*` attrs are only known dynamically (dict keys, not literal kwargs), so pyrefly can't
    # match them against `html.Div`'s typed signature -- `cast` to `Any` rather than fight that.
    div = cast("Any", html.Div)
    return div(
        icon(Icon.DRAG),
        id=component_id,
        draggable="true",
        className=class_name,
        title="Drag to reorder",
        **{f"data-{key}": value for key, value in data_attrs.items()},
    )
