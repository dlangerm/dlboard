# pyright: reportPrivateUsage=false
"""Tests for hover-revealed chart controls: always enabled, hidden until hovered via CSS."""

from __future__ import annotations

from typing import Any

import pandas as pd

from dltrack.conftest import find_props as _find_props
from dltrack.conftest import props as _to_props
from dltrack.models._view import ChartInstance, PanelInstance
from dltrack.plugins.charts.line_chart import LineChart
from dltrack.plugins.pages.experiment import _experiment_page_state as state

LineChart.register(allow_override=True)


def _panel_with_chart() -> PanelInstance[Any, Any]:
    return PanelInstance[Any, Any](
        name="p",
        charts=[ChartInstance[Any, Any](chart_type="line", parameters={"column": "loss", "x_axis": "step"})],
    )


# ---- per-chart edit/delete controls: always enabled, revealed on hover via CSS class ----


def test_render_panel_charts_controls_are_always_enabled_and_hover_revealed() -> None:
    panel = _panel_with_chart()
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})

    [stack] = state.render_panel_charts(panel, df)
    assert _to_props(stack)["className"] == "dl-chart-item"

    controls = _find_props(stack, state.chart_controls_group_id("p", 0))
    assert controls is not None
    assert controls["className"] == "dl-chart-controls"

    drag_handle = _find_props(controls["children"], state.chart_drag_handle_id("p", 0))
    assert drag_handle is not None
    assert drag_handle["draggable"] == "true"

    edit_icon = _find_props(controls["children"], state.edit_chart_button_id("p", 0))
    delete_icon = _find_props(controls["children"], state.delete_chart_button_id("p", 0))
    assert edit_icon is not None
    assert edit_icon.get("disabled") is not True
    assert delete_icon is not None
    assert delete_icon.get("disabled") is not True


def test_panel_header_controls_add_button_is_always_enabled_and_hover_revealed() -> None:
    panel = _panel_with_chart()

    controls = state.panel_header_controls(panel)
    assert _to_props(controls)["className"] == "dl-panel-controls"

    button = _find_props(controls, state.open_chart_button_id("p"))
    assert button is not None
    assert button.get("disabled") is not True


# ---- chart width: packed uses the chart's natural width, grid stretches to its cell ----


def test_render_panel_charts_packed_layout_uses_natural_width() -> None:
    panel = _panel_with_chart()
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})

    [stack] = state.render_panel_charts(panel, df)

    assert _to_props(stack)["w"] == panel.charts[0].natural_width()


def test_render_panel_charts_grid_layout_stretches_to_full_width() -> None:
    panel = _panel_with_chart().model_copy(update={"layout": "grid"})
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})

    [stack] = state.render_panel_charts(panel, df)

    assert _to_props(stack)["w"] == "100%"
