# pyright: reportPrivateUsage=false
"""Tests for hover-revealed chart controls: always enabled, hidden until hovered via CSS."""

from __future__ import annotations

from typing import Any

import pandas as pd

from dlboard.conftest import find_props as _find_props
from dlboard.conftest import props as _to_props
from dlboard.models._view import ChartInstance, PanelInstance
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.serve._pages._experiment import _experiment_page_state as state

LineChart.register(allow_override=True)
TableChart.register(allow_override=True)


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


# ---- unscoped-fetch badge: visible (not hover-gated) when a chart can't hint its columns ----


def _header_row(stack: Any) -> Any:  # noqa: ANN401
    """The chart item's top row: the unscoped-fetch badge (if any) plus the hover-controls group."""
    return _to_props(stack)["children"][0]


def test_render_panel_charts_no_badge_when_chart_hints_its_columns() -> None:
    panel = _panel_with_chart()  # a line chart, always hints {column, x_axis}
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})

    [stack] = state.render_panel_charts(panel, df)
    header_row = _to_props(_header_row(stack))

    assert header_row["justify"] == "flex-end"
    assert len(header_row["children"]) == 1  # just the hover-controls group, no badge


def test_render_panel_charts_badge_when_chart_cannot_hint_its_columns() -> None:
    """A table chart with no `metrics` filter set (its documented "show everything" default)
    can't name specific columns to fetch -- this is exactly the case that logs `fetch_panel_
    dataframe`'s "fetching every metric" debug trace, so the UI should surface it too."""
    panel = PanelInstance[Any, Any](
        name="p", charts=[ChartInstance[Any, Any](chart_type="table", parameters={})]
    )
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})

    [stack] = state.render_panel_charts(panel, df)
    header_row = _to_props(_header_row(stack))

    assert header_row["justify"] == "space-between"
    badge, hover_controls = header_row["children"]
    assert _to_props(hover_controls)["className"] == "dl-chart-controls"
    assert "fetches all metrics" in str(_to_props(badge))


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
