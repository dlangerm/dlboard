"""Tests for hover-revealed chart controls: always enabled, hidden until hovered via CSS."""

from __future__ import annotations

from typing import Any, Literal

import pandas as pd
import pytest

from dlboard.conftest import find_props as _find_props
from dlboard.conftest import props as _to_props
from dlboard.models._view import ChartInstance, PanelInstance
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.serve._pages._experiment import _chart_view as chart_view
from dlboard.serve._pages._experiment import _panel_view as panel_view

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

    [stack] = chart_view.render_panel_charts(panel, df)
    assert _to_props(stack)["className"] == "dl-chart-item"

    controls = _find_props(stack, chart_view.CHART_CONTROLS_GROUP.of("p", 0))
    assert controls is not None
    assert controls["className"] == "dl-chart-controls"

    drag_handle = _find_props(controls["children"], chart_view.CHART_DRAG_HANDLE.of("p", 0))
    assert drag_handle is not None
    assert drag_handle["draggable"] == "true"

    edit_icon = _find_props(controls["children"], chart_view.EDIT_CHART_BUTTON.of("p", 0))
    delete_icon = _find_props(controls["children"], chart_view.DELETE_CHART_BUTTON.of("p", 0))
    assert edit_icon is not None
    assert edit_icon.get("disabled") is not True
    assert delete_icon is not None
    assert delete_icon.get("disabled") is not True


def test_panel_header_controls_add_button_is_always_enabled_and_hover_revealed() -> None:
    panel = _panel_with_chart()

    controls = panel_view.panel_header_controls(panel)
    assert _to_props(controls)["className"] == "dl-panel-controls"

    button = _find_props(controls, panel_view.OPEN_CHART_BUTTON.of("p"))
    assert button is not None
    assert button.get("disabled") is not True


# ---- unscoped-fetch badge: visible (not hover-gated) when a chart can't hint its columns ----


def _chart_item_parts(stack: Any) -> list[Any]:  # noqa: ANN401
    """A chart item's children: the unscoped-fetch badge row (if any), the hover controls, the chart, then its draft slot."""
    return _to_props(stack)["children"]


def test_render_panel_charts_no_badge_when_chart_hints_its_columns() -> None:
    panel = _panel_with_chart()  # a line chart, always hints {column, x_axis}
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})

    [stack] = chart_view.render_panel_charts(panel, df)
    hover_controls, _chart, _draft = _chart_item_parts(stack)  # no badge row above the controls

    assert _to_props(hover_controls)["className"] == "dl-chart-controls"
    assert _to_props(hover_controls)["pos"] == "absolute"  # laid over the chart, not a row of its own


@pytest.mark.parametrize(
    ("parameters", "badged"),
    [
        pytest.param({"rows": "field", "row_field": "step"}, True, id="every-step-of-every-metric"),
        pytest.param({"aggregate": "mean"}, True, id="a-mean-reads-every-step"),
        pytest.param({}, False, id="one-value-per-run"),
    ],
)
def test_render_panel_charts_badge_only_when_an_unscoped_chart_reads_every_step(
    parameters: dict[str, str], *, badged: bool
) -> None:
    """A table chart with no `metrics` filter set (its documented "show everything" default) can't name the
    columns to fetch, which only costs what the badge warns of if it reads every step of them."""
    panel = PanelInstance[Any, Any](
        name="p", charts=[ChartInstance[Any, Any](chart_type="table", parameters=parameters)]
    )
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})

    [stack] = chart_view.render_panel_charts(panel, df)
    parts = _chart_item_parts(stack)

    assert len(parts) == (4 if badged else 3)
    if badged:
        assert "fetches all metrics" in str(_to_props(parts[0]))


# ---- chart width: packed uses the chart's natural width, grid stretches to its cell ----


@pytest.mark.parametrize("layout", ["packed", "grid"])
def test_render_panel_charts_width_follows_the_layout(layout: Literal["packed", "grid"]) -> None:
    """Packed uses the chart's natural width; grid stretches to its cell."""
    panel = _panel_with_chart().model_copy(update={"layout": layout})
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})

    [stack] = chart_view.render_panel_charts(panel, df)

    assert _to_props(stack)["w"] == (panel.charts[0].natural_width() if layout == "packed" else "100%")
