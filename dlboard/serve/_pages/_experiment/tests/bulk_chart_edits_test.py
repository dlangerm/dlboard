"""Which charts a bulk delete of a panel's charts reaches."""

from __future__ import annotations

from typing import Any

from dlboard import models
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve._pages._experiment._bulk_chart_edits import BulkTarget, edit_matching

LineChart.register(allow_override=True)


def _chart(chart_type: str, **parameters: Any) -> models.ChartInstance[Any, Any]:  # noqa: ANN401
    return models.ChartInstance(chart_type=chart_type, parameters=parameters)


def test_only_the_charts_a_query_matches_in_the_panel_it_names_are_deleted() -> None:
    loss, acc = _chart("line", column="loss", x_axis="step"), _chart("line", column="acc", x_axis="step")
    panels = [
        models.PanelInstance[Any, Any](name="a", charts=[loss, acc]),
        models.PanelInstance[Any, Any](name="b", charts=[loss]),
    ]

    def none(_chart: models.ChartInstance[Any, Any]) -> list[models.ChartInstance[Any, Any]]:
        return []

    deleted = edit_matching(BulkTarget(panel="a", query="loss"), none)(panels)

    assert [[c.parameters["column"] for c in p.charts] for p in deleted] == [["acc"], ["loss"]]
