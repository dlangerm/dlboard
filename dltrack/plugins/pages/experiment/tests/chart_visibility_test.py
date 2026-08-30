# pyright: reportPrivateUsage=false
"""Tests for hover-revealed chart controls: always enabled, hidden until hovered via CSS."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import dash_mantine_components as dmc
import pandas as pd

from dltrack import models
from dltrack.conftest import find_props as _find_props
from dltrack.conftest import props as _to_props
from dltrack.models._view import ChartInstance, PanelInstance
from dltrack.plugins.charts.line_chart import LineChart
from dltrack.plugins.pages.experiment import _experiment_page_state as state
from dltrack.plugins.pages.experiment import _header_actions

if TYPE_CHECKING:
    from dltrack.plugins.data_stores.sqlite import SQLLiteStore

_TS = datetime(2026, 1, 1, tzinfo=UTC)

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

    [move_left_icon, move_right_icon, edit_icon, delete_icon] = controls["children"]
    # a single chart has no neighbor to swap with, so move controls stay disabled regardless
    assert _to_props(move_left_icon)["disabled"] is True
    assert _to_props(move_right_icon)["disabled"] is True
    assert _to_props(edit_icon).get("disabled") is not True
    assert _to_props(delete_icon).get("disabled") is not True


def test_render_panel_content_add_button_is_always_enabled_and_hover_revealed(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"loss": 0.5}, step=0, experiment_id=experiment_id, run_id=run.id, timestamp_utc=_TS
            )
        ]
    )
    panel = _panel_with_chart()

    button = _find_props(
        state.render_panel_content(store, experiment_id, panel, {}), state.open_chart_button_id("p")
    )

    assert button is not None
    assert button.get("disabled") is not True
    assert button["className"] == "dl-add-chart-btn"


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


# ---- header actions: no global edit switch, rendered once and never regenerated ----


def _contains_switch(component: object) -> bool:
    if isinstance(component, dmc.Switch):
        return True
    children: object = getattr(component, "children", None)
    if isinstance(children, list):
        return any(_contains_switch(cast("object", c)) for c in cast("list[Any]", children))
    if children is not None:
        return _contains_switch(children)
    return False


def test_header_actions_has_no_edit_switch() -> None:
    assert not _contains_switch(_header_actions())
