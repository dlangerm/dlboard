# pyright: reportPrivateUsage=false
"""Tests for edit-mode-conditional visibility: per-chart controls, the add-chart button, header."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest

from dltrack import models
from dltrack.conftest import find_props as _find_props
from dltrack.conftest import props as _to_props
from dltrack.models import constants
from dltrack.models._view import ChartInstance, PanelInstance
from dltrack.plugins.charts.line_chart import LineChart
from dltrack.plugins.pages.simple_experiment_page import (
    EDIT_MODE_ID,
    _chart_controls_group_id,
    _header_actions,
    _open_chart_button_id,
    _render_panel_charts,
    _render_panel_content,
)

if TYPE_CHECKING:
    from dltrack.plugins.data_stores.sqlite import SQLLiteStore

_TS = datetime(2026, 1, 1, tzinfo=UTC)

LineChart.register(allow_override=True)


def _panel_with_chart() -> PanelInstance[Any, Any]:
    return PanelInstance[Any, Any](
        name="p",
        charts=[ChartInstance[Any, Any](chart_type="line", parameters={"column": "loss", "x_axis": "step"})],
    )


# ---- per-chart edit/delete controls: hidden (not just disabled) outside edit mode ----


@pytest.mark.parametrize("edit_mode", [False, True])
def test_render_panel_charts_controls_visibility_matches_edit_mode(*, edit_mode: bool) -> None:
    panel = _panel_with_chart()
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})

    [stack] = _render_panel_charts(panel, df, edit_mode=edit_mode)
    controls = _find_props(stack, _chart_controls_group_id("p", 0))
    assert controls is not None

    expected_style = {} if edit_mode else {"display": "none"}
    assert controls["style"] == expected_style

    [move_left_icon, move_right_icon, edit_icon, delete_icon] = controls["children"]
    # a single chart has no neighbor to swap with, so move controls stay disabled either way
    assert _to_props(move_left_icon)["disabled"] is True
    assert _to_props(move_right_icon)["disabled"] is True
    assert _to_props(edit_icon)["disabled"] is not edit_mode
    assert _to_props(delete_icon)["disabled"] is not edit_mode


def test_render_panel_content_add_button_visibility_matches_edit_mode(
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

    off = _find_props(
        _render_panel_content(store, experiment_id, panel, {}, edit_mode=False), _open_chart_button_id("p")
    )
    on = _find_props(
        _render_panel_content(store, experiment_id, panel, {}, edit_mode=True), _open_chart_button_id("p")
    )

    assert off is not None
    assert off["disabled"] is True
    assert off["style"] == {"display": "none"}

    assert on is not None
    assert on["disabled"] is False
    assert on["style"] == {}


# ---- header actions: Runs + Edit switch, rendered once and never regenerated ----


def test_header_actions_has_runs_button_and_edit_switch_defaulted_off() -> None:
    actions = _header_actions()

    runs_button = _find_props(actions, constants.HPARAM_DRAWER_TOGGLE_ID)
    assert runs_button is not None

    switch = _find_props(actions, EDIT_MODE_ID)
    assert switch is not None
    assert switch["checked"] is False
