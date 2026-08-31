# pyright: reportPrivateUsage=false
"""Tests for the panel accordion header (hover controls) and the panel-mutation helpers.

Sync/layout/rename/drag/delete all render as hover-revealed controls on each panel's own accordion
header (`panel_header`/`panel_header_controls`) -- true DOM siblings of `AccordionControl` there,
not nested inside its native `<button>`. There's no separate "Manage panels" drawer/list anymore.
Reordering is drag-and-drop (`_experiment_page_dragdrop.js`), not up/down buttons.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import dash_mantine_components as dmc
from dash._utils import to_json  # pyright: ignore[reportUnknownVariableType]

from dltrack.conftest import find_props as _find_props
from dltrack.models._view import ChartInstance, PanelInstance
from dltrack.plugins.charts.line_chart import LineChart
from dltrack.plugins.pages.experiment import _experiment_page_state as state

if TYPE_CHECKING:
    from dltrack.plugins.data_stores.sqlite import SQLLiteStore

LineChart.register(allow_override=True)

# ---- panel header: hover-revealed sync/layout/rename/drag/delete, siblings of AccordionControl ----


def test_panel_header_contains_accordion_control_and_hover_controls() -> None:
    panel = PanelInstance[Any, Any](name="train")

    rendered = state.panel_header(panel)

    assert _find_props(rendered, state.delete_panel_button_id("train")) is not None
    assert _find_props(rendered, state.panel_drag_handle_id("train")) is not None
    assert _find_props(rendered, state.rename_panel_button_id("train")) is not None
    assert _find_props(rendered, state.panel_sync_switch_id("train")) is not None
    assert _find_props(rendered, state.panel_layout_control_id("train")) is not None


def test_panel_header_controls_delete_is_always_enabled() -> None:
    controls = state.panel_header_controls(PanelInstance[Any, Any](name="train"))

    delete_button = _find_props(controls, state.delete_panel_button_id("train"))
    assert delete_button is not None
    assert delete_button.get("disabled") is not True


def test_panel_header_controls_drag_handle_carries_the_panel_name() -> None:
    controls = state.panel_header_controls(PanelInstance[Any, Any](name="train"))

    handle = _find_props(controls, state.panel_drag_handle_id("train"))
    assert handle is not None
    assert handle["draggable"] == "true"
    assert handle["data-panel-name"] == "train"


def test_panel_header_controls_shows_sync_and_layout_state() -> None:
    synced = state.panel_header_controls(PanelInstance[Any, Any](name="p", sync=True, layout="grid"))
    unsynced = state.panel_header_controls(PanelInstance[Any, Any](name="p", sync=False, layout="packed"))

    assert _find_props(synced, state.panel_sync_switch_id("p"))["checked"] is True  # pyright: ignore[reportOptionalSubscript]
    assert _find_props(synced, state.panel_layout_control_id("p"))["value"] == "grid"  # pyright: ignore[reportOptionalSubscript]
    assert _find_props(unsynced, state.panel_sync_switch_id("p"))["checked"] is False  # pyright: ignore[reportOptionalSubscript]
    assert _find_props(unsynced, state.panel_layout_control_id("p"))["value"] == "packed"  # pyright: ignore[reportOptionalSubscript]


# ---- reorder_panel ----


def test_reorder_panel_moves_before_target() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b", "c")]
    result = state.reorder_panel(panels, "c", "a", after=False)
    assert [p.name for p in result] == ["c", "a", "b"]


def test_reorder_panel_moves_after_target() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b", "c")]
    result = state.reorder_panel(panels, "a", "b", after=True)
    assert [p.name for p in result] == ["b", "a", "c"]


def test_reorder_panel_same_panel_is_a_noop() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b", "c")]
    assert state.reorder_panel(panels, "a", "a", after=True) == panels


def test_reorder_panel_unknown_panel_is_a_noop() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b")]
    assert state.reorder_panel(panels, "missing", "a", after=True) == panels
    assert state.reorder_panel(panels, "a", "missing", after=True) == panels


# ---- panel sync: switch + set_panel_sync + _apply_panel_sync ----


def test_set_panel_sync_updates_only_the_named_panel() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b")]

    result = state.set_panel_sync(panels, "a", sync=False)

    by_name = {p.name: p.sync for p in result}
    assert by_name == {"a": False, "b": True}


def test_apply_panel_sync_drops_sync_id_when_disabled() -> None:
    chart = dmc.LineChart(
        data=[], dataKey="step", series=[], lineChartProps={"syncId": "step", "syncMethod": "value"}
    )

    result = state._apply_panel_sync(chart, panel_name="train", sync=False)

    assert "syncId" not in result.lineChartProps  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    assert result.lineChartProps["syncMethod"] == "value"  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]


# ---- panel layout: segmented control + set_panel_layout ----


def test_set_panel_layout_updates_only_the_named_panel() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b")]

    result = state.set_panel_layout(panels, "a", "grid")

    by_name = {p.name: p.layout for p in result}
    assert by_name == {"a": "grid", "b": "packed"}


def test_apply_panel_sync_scopes_sync_id_to_the_panel_when_enabled() -> None:
    chart = dmc.LineChart(data=[], dataKey="step", series=[], lineChartProps={"syncId": "step"})

    result = state._apply_panel_sync(chart, panel_name="train", sync=True)

    assert result.lineChartProps["syncId"] == "train:step"  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]


def test_apply_panel_sync_scoping_keeps_different_panels_apart() -> None:
    """The whole point: two panels sharing the same x-axis name (e.g. "step") must not sync."""
    train_chart = dmc.LineChart(data=[], dataKey="step", series=[], lineChartProps={"syncId": "step"})
    val_chart = dmc.LineChart(data=[], dataKey="step", series=[], lineChartProps={"syncId": "step"})

    train_result = state._apply_panel_sync(train_chart, panel_name="train", sync=True)
    val_result = state._apply_panel_sync(val_chart, panel_name="val", sync=True)

    assert train_result.lineChartProps["syncId"] != val_result.lineChartProps["syncId"]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]


def test_apply_panel_sync_ignores_non_line_charts() -> None:
    other = dmc.Text("not a line chart")

    assert state._apply_panel_sync(other, panel_name="train", sync=False) is other


# ---- reorder_chart ----


def _panel_with_charts(*names: str) -> PanelInstance[Any, Any]:
    charts = [
        ChartInstance[Any, Any](chart_type="line", parameters={"column": n, "x_axis": "step"}) for n in names
    ]
    return PanelInstance[Any, Any](name="p", charts=charts)


def test_reorder_chart_moves_before_target() -> None:
    panels = [_panel_with_charts("a", "b", "c", "d")]
    result = state.reorder_chart(panels, "p", 0, 2, after=False)
    assert [c.parameters["column"] for c in result[0].charts] == ["b", "a", "c", "d"]


def test_reorder_chart_moves_after_target() -> None:
    panels = [_panel_with_charts("a", "b", "c", "d")]
    result = state.reorder_chart(panels, "p", 3, 1, after=True)
    assert [c.parameters["column"] for c in result[0].charts] == ["a", "b", "d", "c"]


def test_reorder_chart_same_index_is_a_noop() -> None:
    panels = [_panel_with_charts("a", "b", "c")]
    result = state.reorder_chart(panels, "p", 1, 1, after=True)
    assert [c.parameters["column"] for c in result[0].charts] == ["a", "b", "c"]


def test_reorder_chart_out_of_bounds_index_is_a_noop() -> None:
    panels = [_panel_with_charts("a", "b", "c")]
    assert [
        c.parameters["column"] for c in state.reorder_chart(panels, "p", 5, 0, after=False)[0].charts
    ] == [
        "a",
        "b",
        "c",
    ]
    assert [
        c.parameters["column"] for c in state.reorder_chart(panels, "p", 0, 5, after=False)[0].charts
    ] == [
        "a",
        "b",
        "c",
    ]


def test_reorder_chart_unknown_panel_is_a_noop() -> None:
    panels = [_panel_with_charts("a", "b")]
    result = state.reorder_chart(panels, "missing", 0, 1, after=True)
    assert [c.parameters["column"] for c in result[0].charts] == ["a", "b"]


# ---- add_chart_to_panel_by_name ----


def test_add_chart_to_panel_by_name_creates_panel_when_missing() -> None:
    chart = ChartInstance[Any, Any](chart_type="line", parameters={"column": "loss", "x_axis": "step"})

    panels = state.add_chart_to_panel_by_name([], "train", chart)

    assert [p.name for p in panels] == ["train"]
    assert panels[0].charts == [chart]


def test_add_chart_to_panel_by_name_appends_to_existing_panel() -> None:
    existing_chart = ChartInstance[Any, Any](
        chart_type="line", parameters={"column": "loss", "x_axis": "step"}
    )
    new_chart = ChartInstance[Any, Any](chart_type="line", parameters={"column": "acc", "x_axis": "step"})
    panels = [
        PanelInstance[Any, Any](name="train", charts=[existing_chart]),
        PanelInstance[Any, Any](name="val"),
    ]

    result = state.add_chart_to_panel_by_name(panels, "train", new_chart)

    by_name = {p.name: p for p in result}
    assert by_name["train"].charts == [existing_chart, new_chart]
    assert by_name["val"].charts == []


# ---- reorder_rendered_panels / reorder_rendered_charts: reorder-in-place, no refetch ----


def _wire_container(store: SQLLiteStore, experiment_id: int) -> dict[str, Any]:
    """
    `accordion_view`'s container round-tripped through Dash's own JSON encoder.

    A callback's `State(METRIC_CONTENT_ID, "children")` never sees the live `Component` objects
    `accordion_view` returns -- Dash serializes them to plain `{"type", "namespace", "props"}`
    dicts on the wire, and that's the shape `reorder_rendered_panels`/`reorder_rendered_charts`
    actually operate on in the real callback. Round-tripping through the real encoder here (rather
    than hand-building a fake dict) is what keeps this test honest about that shape.
    """
    _new_panel_group, container = state.accordion_view(store, experiment_id)
    serialized = to_json(container)
    assert isinstance(serialized, str)
    return cast("dict[str, Any]", json.loads(serialized))


def test_reorder_rendered_panels_reorders_accordion_items(store: SQLLiteStore, experiment_id: int) -> None:
    page = store.get_or_create_page(state.BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(
        page.model_copy(
            update={
                "panels": [PanelInstance[Any, Any](name=n) for n in ("a", "b", "c")],
                "page_settings": {state.OPEN_PANEL_KEY: []},
            }
        )
    )
    container = _wire_container(store, experiment_id)

    reordered = state.reorder_rendered_panels(container, ["c", "a", "b"])

    accordion = reordered["props"]["children"][0]
    assert [item["props"]["value"] for item in accordion["props"]["children"]] == ["c", "a", "b"]


def _find_prop_by_key(node: Any, key: str) -> dict[str, Any] | None:  # noqa: ANN401
    """Depth-first search a JSON-encoded (wire-format) Dash component tree for the first props dict
    (of any component) that has `key` set."""
    if isinstance(node, list):
        for item in cast("list[Any]", node):
            found = _find_prop_by_key(item, key)
            if found is not None:
                return found
        return None
    if not isinstance(node, dict) or "props" not in node:
        return None
    props = cast("dict[str, Any]", node)["props"]
    if key in props:
        return props
    return _find_prop_by_key(props.get("children"), key)


def test_reorder_rendered_charts_reorders_chart_items_within_a_panel(
    store: SQLLiteStore, experiment_id: int
) -> None:
    charts = [
        ChartInstance[Any, Any](chart_type="line", parameters={"column": n, "x_axis": "step"})
        for n in ("a", "b", "c")
    ]
    page = store.get_or_create_page(state.BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(
        page.model_copy(
            update={
                "panels": [PanelInstance[Any, Any](name="p", charts=charts)],
                "page_settings": {state.OPEN_PANEL_KEY: ["p"]},
            }
        )
    )
    container = _wire_container(store, experiment_id)

    # Same permutation `move_index` would compute for dragging chart 0 to sit after chart 2.
    order = state.move_index([0, 1, 2], 0, 2, after=True)
    reordered = state.reorder_rendered_charts(container, "p", order)

    accordion = reordered["props"]["children"][0]
    item = next(i for i in accordion["props"]["children"] if i["props"]["value"] == "p")
    chart_items = item["props"]["children"][1]["props"]["children"]["props"]["children"]["props"]["children"]
    # Each item's drag handle still carries its *original* chart index -- reordering the rendered
    # tree doesn't (and shouldn't) touch that -- so the sequence of original indices across the
    # reordered items is exactly `order` if the splice actually worked.
    original_indices: list[int] = []
    for chart_item in chart_items:
        handle_props = _find_prop_by_key(chart_item, "data-chart-index")
        assert handle_props is not None
        original_indices.append(int(handle_props["data-chart-index"]))
    assert original_indices == order
