"""Which charts a bulk edit of a panel's charts reaches, and what it does to them."""

from __future__ import annotations

from typing import Any

import pytest

from dlboard import models
from dlboard.plugins.charts.bar_chart import BarChart
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.serve._pages._experiment._bulk_chart_edits import BulkTarget, _edit_matching, _with_x_axis

LineChart.register(allow_override=True)
BarChart.register(allow_override=True)
TableChart.register(allow_override=True)


def _chart(chart_type: str, **parameters: Any) -> models.ChartInstance[Any, Any]:  # noqa: ANN401
    return models.ChartInstance(chart_type=chart_type, parameters=parameters)


@pytest.mark.parametrize(
    ("chart", "changed"),
    [
        pytest.param(_chart("line", column="loss", x_axis="step"), True, id="line"),
        pytest.param(_chart("bar", column="loss", x_axis="hidden"), True, id="bar"),
        pytest.param(
            _chart("table", show_metrics=["loss"], show_hparams=[]), False, id="a-type-with-no-x-axis"
        ),
        pytest.param(_chart("gone", whatever=1), False, id="a-type-that-is-not-installed"),
        pytest.param(
            _chart("line", column="loss", x_axis="step", max_axis_label_chars=1),
            False,
            id="settings-that-do-not-validate",
        ),
    ],
)
def test_an_x_axis_is_set_only_on_the_charts_that_can_take_one(
    chart: models.ChartInstance[Any, Any], *, changed: bool
) -> None:
    after = _with_x_axis(chart, "epoch")

    assert (after.parameters.get("x_axis") == "epoch") is changed
    assert after.id == chart.id  # the same chart, so a link to it still opens it
    if not changed:
        assert after == chart


def test_only_the_charts_a_query_matches_in_the_panel_it_names_are_replaced() -> None:
    loss, acc = _chart("line", column="loss", x_axis="step"), _chart("line", column="acc", x_axis="step")
    panels = [
        models.PanelInstance[Any, Any](name="a", charts=[loss, acc]),
        models.PanelInstance[Any, Any](name="b", charts=[loss]),
    ]

    deleted = _edit_matching(BulkTarget(panel="a", query="loss"), lambda _chart: [])(panels)
    edited = _edit_matching(BulkTarget(panel="a", query="acc"), lambda chart: [_with_x_axis(chart, "epoch")])(
        panels
    )

    assert [[c.parameters["column"] for c in p.charts] for p in deleted] == [["acc"], ["loss"]]
    assert [[c.parameters["x_axis"] for c in p.charts] for p in edited] == [["step", "epoch"], ["step"]]
