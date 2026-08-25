# pyright: reportPrivateUsage=false
"""A chart that fails to render must not take the rest of its panel down with it.

Regression: a suggested/auto-generated chart referencing a column that isn't actually present
(e.g. `experiment_id`, or any bad manual edit) used to raise out of `_render_panel_charts` and
blow up the whole panel render — including any other, perfectly fine charts sharing the panel.
"""

from __future__ import annotations

from typing import Any

import dash_mantine_components as dmc
import pandas as pd

from dltrack.conftest import props as _props
from dltrack.models._view import ChartInstance, PanelInstance
from dltrack.plugins.charts.line_chart import LineChart
from dltrack.plugins.pages.simple_experiment_page import _render_panel_charts

LineChart.register(allow_override=True)


def test_broken_chart_renders_as_error_alert_without_raising() -> None:
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})
    broken = ChartInstance[Any, Any](
        chart_type="line", parameters={"column": "does_not_exist", "x_axis": "step"}
    )
    panel = PanelInstance[Any, Any](name="p", charts=[broken])

    [stack] = _render_panel_charts(panel, df)

    rendered = _props(stack)["children"][1]
    assert isinstance(rendered, dmc.Alert)
    assert _props(rendered)["color"] == "red"
    assert "does_not_exist" in str(_props(rendered)["children"])


def test_broken_chart_does_not_prevent_sibling_charts_from_rendering() -> None:
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})
    broken = ChartInstance[Any, Any](
        chart_type="line", parameters={"column": "does_not_exist", "x_axis": "step"}
    )
    working = ChartInstance[Any, Any](chart_type="line", parameters={"column": "loss", "x_axis": "step"})
    panel = PanelInstance[Any, Any](name="p", charts=[broken, working])

    stacks = _render_panel_charts(panel, df)

    assert len(stacks) == 2
    broken_rendered = _props(stacks[0])["children"][1]
    working_rendered = _props(stacks[1])["children"][1]
    assert isinstance(broken_rendered, dmc.Alert)
    assert isinstance(working_rendered, dmc.LineChart)
