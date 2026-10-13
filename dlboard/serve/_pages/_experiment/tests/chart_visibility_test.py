"""Tests for hover-revealed chart controls: always enabled, hidden until hovered via CSS."""

from __future__ import annotations

from typing import Any, Literal

import pandas as pd
import pytest

from dlboard.conftest import find_props as _find_props
from dlboard.conftest import props as _to_props
from dlboard.models._view import ChartInstance, PanelInstance
from dlboard.plugins.charts.bar_chart import BarChart
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.serve._pages._experiment import _experiment_page_state as state

BarChart.register(allow_override=True)
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
    """The chart item's top row: the chart title and unscoped-fetch badge (if any) plus the hover-controls group."""
    return _to_props(stack)["children"][0]


@pytest.mark.parametrize(
    ("chart_type", "parameters"),
    [
        ("line", {"column": "DeviceStatsMonitor.on_test_batch_end/active.all.allocated", "x_axis": "step"}),
        ("bar", {"column": "DeviceStatsMonitor.on_test_batch_end/active.all.allocated", "x_axis": "step"}),
    ],
)
def test_render_panel_charts_titles_a_metric_chart_with_its_full_metric_name(
    chart_type: str, parameters: dict[str, str]
) -> None:
    """The axis titles are shortened to fit; the header above the chart carries the whole name."""
    panel = PanelInstance[Any, Any](
        name="p", charts=[ChartInstance[Any, Any](chart_type=chart_type, parameters=parameters)]
    )
    df = pd.DataFrame({"run_id": [1], "step": [0], parameters["column"]: [0.5]})

    [stack] = state.render_panel_charts(panel, df)
    header_row = _to_props(_header_row(stack))

    title, hover_controls = header_row["children"]  # no badge: a metric chart always hints its columns
    assert _to_props(title)["children"] == parameters["column"]
    assert _to_props(hover_controls)["className"] == "dl-chart-controls"


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


@pytest.mark.parametrize("layout", ["packed", "grid"])
def test_render_panel_charts_width_follows_the_layout(layout: Literal["packed", "grid"]) -> None:
    """Packed uses the chart's natural width; grid stretches to its cell."""
    panel = _panel_with_chart().model_copy(update={"layout": layout})
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})

    [stack] = state.render_panel_charts(panel, df)

    assert _to_props(stack)["w"] == (panel.charts[0].natural_width() if layout == "packed" else "100%")
