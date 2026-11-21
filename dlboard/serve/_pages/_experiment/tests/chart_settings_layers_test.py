"""What a panel's or a page's chart settings form offers, and what it writes back."""

from __future__ import annotations

from dlboard.plugins.charts.bar_chart import BarChart
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve._pages._experiment._chart_settings_layers import layerable_fields, layered

LineChart.register(allow_override=True)
BarChart.register(allow_override=True)


def test_the_form_offers_every_setting_its_charts_types_may_have_set_once_by_name() -> None:
    lines, both = layerable_fields(["line", "line"]), layerable_fields(["line", "bar", "not-installed"])

    assert {"smoothing", "height", "x_axis"} <= lines.keys()
    assert "column" not in lines  # what a chart draws is its own
    assert both.keys() >= lines.keys()  # a type added brings its settings
    assert both.keys() - lines.keys() == layerable_fields(["bar"]).keys() - lines.keys()


def test_what_the_form_did_not_show_is_kept_for_a_chart_that_has_it_later() -> None:
    current = {"smoothing": 0.6, "height": 400, "orientation": "horizontal"}

    after = layered(current, shown=["height", "smoothing"], chosen={"height": 500})

    # Smoothing was shown and not ticked: no longer set. The bar charts' orientation was not shown (no bar chart now):
    # kept, to draw a bar chart added later with.
    assert after == {"height": 500, "orientation": "horizontal"}
