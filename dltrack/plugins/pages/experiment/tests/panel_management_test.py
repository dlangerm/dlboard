# pyright: reportPrivateUsage=false
"""Tests for the panel accordion header (hover controls) and the panel-mutation helpers.

Sync/layout/rename/drag/delete all render as hover-revealed controls on each panel's own accordion
header (`panel_header`/`panel_header_controls`) -- true DOM siblings of `AccordionControl` there,
not nested inside its native `<button>`. There's no separate "Manage panels" drawer/list anymore.
Reordering is drag-and-drop (`_experiment_page_dragdrop.js`), not up/down buttons.
"""

from __future__ import annotations

from typing import Any

import dash_mantine_components as dmc

from dltrack.conftest import find_props as _find_props
from dltrack.models._view import ChartInstance, PanelInstance
from dltrack.plugins.pages.experiment import _experiment_page_state as state

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
