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

from dlboard.serve._pages._experiment._ids import ExperimentPage
from dlboard.serve._pages._experiment._panel_data import chart_data_fingerprint

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


def _unscoped_fetch_badge(chart: models.ChartInstance[Any, Any]) -> Component | None:
    """
    A small, always-visible warning badge for a chart whose data fetch can't be scoped.

    `chart.hint_required_columns() is None` means this chart's settings don't name specific
    metrics (e.g. a table chart with no `metrics` filter, left at its "show every metric" default)
    -- rendering it means fetching every metric logged for the whole experiment, every time its
    panel updates. That's a real, sometimes-significant cost the user has no other way to notice
    (it only ever shows up as a backend debug log), so it gets a badge instead of hover-only
    controls: this should be visible without having to go looking for it.
    """
    if chart.hint_required_columns() is not None:
        return None
    return dmc.Tooltip(
        dmc.Badge("fetches all metrics", color="yellow", variant="light", size="xs", radius="sm"),
        label="No metrics filter is set, so this chart fetches every metric in the experiment on "
        'every update -- can be slow on a large experiment. Set specific metrics in "Edit chart" '
        "to scope it down.",
        position="top",
        withArrow=True,
        multiline=True,
        w=260,
    )


def chart_body(
    panel: models.PanelInstance[Any, Any], index: int, dataframe: pd.DataFrame, page_size: int
) -> list[Component]:
    """
    What a chart item shows below its controls: the chart, and a hidden node holding the fingerprint it is drawn from.

    The fingerprint travels with the DOM it describes (not in a store the page keeps beside it), so a
    chart mounted any way at all -- first load, a panel opening, a page of charts, an edit -- says what
    it was drawn from, and the poll redraws it only when that is no longer what the data says.
    """
    chart = panel.charts[index]
    context = models.RenderContext(columns=panel.grid_columns, page_size=page_size, chart_id=chart.id)
    rendered = _apply_panel_sync(
        _render_chart_safely(chart, dataframe, panel.name, context), panel_name=panel.name, sync=panel.sync
    )
    fingerprint = html.Div(
        chart_data_fingerprint(chart, dataframe),
        id=CHART_FINGERPRINT.of(panel.name, index),
        hidden=True,
    )
    return [rendered, fingerprint]


def render_chart_item(
    panel: models.PanelInstance[Any, Any],
    index: int,
    dataframe: pd.DataFrame,
    page_size: int = models.DEFAULT_RENDER_CONTEXT.page_size,
) -> Component:
    """
    Render one chart in a panel (edit/delete controls above it, revealed on hover) as its own node.

    Individually addressable via `CHART_CONTENT.of(panel.name, index)`, and its chart (the body, below the
    controls) via `CHART_BODY`: a live-update poll (`poll_for_updates` in `__init__.py`) replaces
    exactly the body of a chart whose own data changed, instead of rebuilding `render_panel_charts`' whole
    shared-dataframe blob for the panel: two charts sharing a panel (a common "Auto-generate charts"
    grouping) commonly have very different update cadences -- an epoch-level validation chart next to a
    per-step training chart -- and rebuilding the *panel* whenever *either* changes would pop the
    untouched one too.
    """
    chart = panel.charts[index]
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
    badge = _unscoped_fetch_badge(chart)
    return dmc.Stack(
        [
            *([dmc.Group([badge])] if badge else []),
            hover_controls,
            html.Div(
                chart_body(panel, index, dataframe, page_size),
                id=CHART_BODY.of(panel.name, index),
            ),
        ],
        id=CHART_CONTENT.of(panel.name, index),
        gap="xs",
        w="100%" if panel.layout == "grid" else chart.natural_width(context),
        maw="100%",
        p="xs",
        pos="relative",
        className="dl-chart-item",
        **cast("dict[str, Any]", {"data-chart-id": chart.id}),
    )


def render_panel_charts(
    panel: models.PanelInstance[Any, Any],
    dataframe: pd.DataFrame,
    indexes: Sequence[int] | None = None,
    page_size: int = models.DEFAULT_RENDER_CONTEXT.page_size,
) -> list[Component]:
    """Render the charts of a panel at `indexes` (every chart, if not given) -- see `render_chart_item`."""
    return [
        render_chart_item(panel, idx, dataframe, page_size)
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
