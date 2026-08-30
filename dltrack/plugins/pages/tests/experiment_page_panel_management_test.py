# pyright: reportPrivateUsage=false
"""Tests for the panel accordion header (hover controls) and the panel-mutation helpers.

Sync/layout/rename/move/delete all render as hover-revealed controls on each panel's own accordion
header (`_panel_header`/`_panel_header_controls`) -- true DOM siblings of `AccordionControl` there,
not nested inside its native `<button>`. There's no separate "Manage panels" drawer/list anymore.
"""

from __future__ import annotations

from typing import Any

import dash_mantine_components as dmc

from dltrack.conftest import find_props as _find_props
from dltrack.models._view import ChartInstance, PanelInstance
from dltrack.plugins.pages.simple_experiment_page import (
    _add_chart_to_panel_by_name,
    _apply_panel_sync,
    _delete_panel_button_id,
    _move_chart,
    _move_panel,
    _move_panel_button_id,
    _panel_header,
    _panel_header_controls,
    _panel_layout_control_id,
    _panel_sync_switch_id,
    _PanelMoveDirection,
    _rename_panel_button_id,
    _set_panel_layout,
    _set_panel_sync,
)

# ---- panel header: hover-revealed sync/layout/rename/move/delete, siblings of AccordionControl ----


def test_panel_header_contains_accordion_control_and_hover_controls() -> None:
    panel = PanelInstance[Any, Any](name="train")

    rendered = _panel_header(panel, index=0, count=1)

    assert _find_props(rendered, _delete_panel_button_id("train")) is not None
    assert _find_props(rendered, _move_panel_button_id("train", "up")) is not None
    assert _find_props(rendered, _rename_panel_button_id("train")) is not None
    assert _find_props(rendered, _panel_sync_switch_id("train")) is not None
    assert _find_props(rendered, _panel_layout_control_id("train")) is not None


def test_panel_header_controls_delete_is_always_enabled() -> None:
    controls = _panel_header_controls(PanelInstance[Any, Any](name="train"), index=0, count=1)

    delete_button = _find_props(controls, _delete_panel_button_id("train"))
    assert delete_button is not None
    assert delete_button.get("disabled") is not True


def test_panel_header_controls_shows_sync_and_layout_state() -> None:
    synced = _panel_header_controls(
        PanelInstance[Any, Any](name="p", sync=True, layout="grid"), index=0, count=1
    )
    unsynced = _panel_header_controls(
        PanelInstance[Any, Any](name="p", sync=False, layout="packed"), index=0, count=1
    )

    assert _find_props(synced, _panel_sync_switch_id("p"))["checked"] is True  # pyright: ignore[reportOptionalSubscript]
    assert _find_props(synced, _panel_layout_control_id("p"))["value"] == "grid"  # pyright: ignore[reportOptionalSubscript]
    assert _find_props(unsynced, _panel_sync_switch_id("p"))["checked"] is False  # pyright: ignore[reportOptionalSubscript]
    assert _find_props(unsynced, _panel_layout_control_id("p"))["value"] == "packed"  # pyright: ignore[reportOptionalSubscript]


def _move_disabled(panel_name: str, *, index: int, count: int, direction: _PanelMoveDirection) -> bool:
    controls = _panel_header_controls(PanelInstance[Any, Any](name=panel_name), index=index, count=count)
    props = _find_props(controls, _move_panel_button_id(panel_name, direction))
    assert props is not None
    return bool(props["disabled"])


def test_panel_header_controls_move_buttons_disabled_at_boundaries() -> None:
    assert _move_disabled("a", index=0, count=3, direction="up") is True
    assert _move_disabled("a", index=0, count=3, direction="down") is False

    assert _move_disabled("b", index=1, count=3, direction="up") is False
    assert _move_disabled("b", index=1, count=3, direction="down") is False

    assert _move_disabled("c", index=2, count=3, direction="up") is False
    assert _move_disabled("c", index=2, count=3, direction="down") is True


# ---- _move_panel ----


def test_move_panel_up_swaps_with_previous() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b", "c")]
    result = _move_panel(panels, "b", "up")
    assert [p.name for p in result] == ["b", "a", "c"]


def test_move_panel_down_swaps_with_next() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b", "c")]
    result = _move_panel(panels, "b", "down")
    assert [p.name for p in result] == ["a", "c", "b"]


def test_move_panel_at_boundary_is_a_noop() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b", "c")]
    assert [p.name for p in _move_panel(panels, "a", "up")] == ["a", "b", "c"]
    assert [p.name for p in _move_panel(panels, "c", "down")] == ["a", "b", "c"]


def test_move_panel_unknown_panel_is_a_noop() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b")]
    assert _move_panel(panels, "missing", "up") == panels


# ---- panel sync: switch + _set_panel_sync + _apply_panel_sync ----


def test_set_panel_sync_updates_only_the_named_panel() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b")]

    result = _set_panel_sync(panels, "a", sync=False)

    by_name = {p.name: p.sync for p in result}
    assert by_name == {"a": False, "b": True}


def test_apply_panel_sync_drops_sync_id_when_disabled() -> None:
    chart = dmc.LineChart(
        data=[], dataKey="step", series=[], lineChartProps={"syncId": "step", "syncMethod": "value"}
    )

    result = _apply_panel_sync(chart, panel_name="train", sync=False)

    assert "syncId" not in result.lineChartProps  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    assert result.lineChartProps["syncMethod"] == "value"  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]


# ---- panel layout: segmented control + _set_panel_layout ----


def test_set_panel_layout_updates_only_the_named_panel() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b")]

    result = _set_panel_layout(panels, "a", "grid")

    by_name = {p.name: p.layout for p in result}
    assert by_name == {"a": "grid", "b": "packed"}


def test_apply_panel_sync_scopes_sync_id_to_the_panel_when_enabled() -> None:
    chart = dmc.LineChart(data=[], dataKey="step", series=[], lineChartProps={"syncId": "step"})

    result = _apply_panel_sync(chart, panel_name="train", sync=True)

    assert result.lineChartProps["syncId"] == "train:step"  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]


def test_apply_panel_sync_scoping_keeps_different_panels_apart() -> None:
    """The whole point: two panels sharing the same x-axis name (e.g. "step") must not sync."""
    train_chart = dmc.LineChart(data=[], dataKey="step", series=[], lineChartProps={"syncId": "step"})
    val_chart = dmc.LineChart(data=[], dataKey="step", series=[], lineChartProps={"syncId": "step"})

    train_result = _apply_panel_sync(train_chart, panel_name="train", sync=True)
    val_result = _apply_panel_sync(val_chart, panel_name="val", sync=True)

    assert train_result.lineChartProps["syncId"] != val_result.lineChartProps["syncId"]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]


def test_apply_panel_sync_ignores_non_line_charts() -> None:
    other = dmc.Text("not a line chart")

    assert _apply_panel_sync(other, panel_name="train", sync=False) is other


# ---- _move_chart ----


def _panel_with_charts(*names: str) -> PanelInstance[Any, Any]:
    charts = [
        ChartInstance[Any, Any](chart_type="line", parameters={"column": n, "x_axis": "step"}) for n in names
    ]
    return PanelInstance[Any, Any](name="p", charts=charts)


def test_move_chart_left_swaps_with_previous() -> None:
    panels = [_panel_with_charts("a", "b", "c")]
    result = _move_chart(panels, "p", 1, "left")
    assert [c.parameters["column"] for c in result[0].charts] == ["b", "a", "c"]


def test_move_chart_right_swaps_with_next() -> None:
    panels = [_panel_with_charts("a", "b", "c")]
    result = _move_chart(panels, "p", 1, "right")
    assert [c.parameters["column"] for c in result[0].charts] == ["a", "c", "b"]


def test_move_chart_at_boundary_is_a_noop() -> None:
    panels = [_panel_with_charts("a", "b", "c")]
    assert [c.parameters["column"] for c in _move_chart(panels, "p", 0, "left")[0].charts] == ["a", "b", "c"]
    assert [c.parameters["column"] for c in _move_chart(panels, "p", 2, "right")[0].charts] == ["a", "b", "c"]


def test_move_chart_unknown_panel_is_a_noop() -> None:
    panels = [_panel_with_charts("a", "b")]
    result = _move_chart(panels, "missing", 0, "left")
    assert [c.parameters["column"] for c in result[0].charts] == ["a", "b"]


# ---- _add_chart_to_panel_by_name ----


def test_add_chart_to_panel_by_name_creates_panel_when_missing() -> None:
    chart = ChartInstance[Any, Any](chart_type="line", parameters={"column": "loss", "x_axis": "step"})

    panels = _add_chart_to_panel_by_name([], "train", chart)

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

    result = _add_chart_to_panel_by_name(panels, "train", new_chart)

    by_name = {p.name: p for p in result}
    assert by_name["train"].charts == [existing_chart, new_chart]
    assert by_name["val"].charts == []
