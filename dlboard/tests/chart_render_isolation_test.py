"""A chart that fails to render must not take the rest of its panel down with it.

Regression: a suggested/auto-generated chart referencing a column that isn't actually present
(e.g. `experiment_id`, or any bad manual edit) used to raise out of `_render_panel_charts` and
blow up the whole panel render — including any other, perfectly fine charts sharing the panel.
"""

from __future__ import annotations

from typing import Any

import dash_mantine_components as dmc
import pandas as pd
import pytest

from dlboard.conftest import props as _props
from dlboard.models._view import ChartInstance, PanelInstance
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve._pages._experiment._chart_view import (
    render_panel_charts as _render_panel_charts,
)

LineChart.register(allow_override=True)


def _chart_in(item: Any) -> Any:  # noqa: ANN401
    """The chart a rendered chart item shows: the first thing in its body, the last part of the item."""
    body: Any = _props(item)["children"][-1]
    return _props(body)["children"][0]


@pytest.mark.parametrize(
    ("broken", "named_in_alert"),
    [
        # A chart referencing a column that isn't present (e.g. a bad manual edit).
        pytest.param(
            ChartInstance[Any, Any](
                chart_type="line", parameters={"column": "does_not_exist", "x_axis": "step"}
            ),
            "does_not_exist",
            id="missing-column",
        ),
        # A saved view can name a chart type this deployment has no plugin for (removed, or never
        # installed here) -- same isolation guarantee, not a 500 that takes the panel down with it.
        pytest.param(
            ChartInstance[Any, Any](chart_type="not-installed", parameters={}),
            "not-installed",
            id="missing-plugin",
        ),
    ],
)
def test_broken_chart_renders_as_error_alert_without_raising(
    broken: ChartInstance[Any, Any], named_in_alert: str
) -> None:
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})
    panel = PanelInstance[Any, Any](name="p", charts=[broken])

    [stack] = _render_panel_charts(panel, df)

    rendered = _chart_in(stack)
    assert isinstance(rendered, dmc.Alert)
    assert _props(rendered)["color"] == "red"
    assert named_in_alert in str(_props(rendered)["children"])


def test_broken_chart_does_not_prevent_sibling_charts_from_rendering() -> None:
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})
    broken = ChartInstance[Any, Any](
        chart_type="line", parameters={"column": "does_not_exist", "x_axis": "step"}
    )
    working = ChartInstance[Any, Any](chart_type="line", parameters={"column": "loss", "x_axis": "step"})
    panel = PanelInstance[Any, Any](name="p", charts=[broken, working])

    stacks = _render_panel_charts(panel, df)

    assert len(stacks) == 2
    broken_rendered = _chart_in(stacks[0])
    working_rendered = _chart_in(stacks[1])
    assert isinstance(broken_rendered, dmc.Alert)
    assert isinstance(working_rendered, dmc.Stack)
