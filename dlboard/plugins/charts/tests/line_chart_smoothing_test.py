"""A line chart's smoothing (TensorBoard's, over every point read) and its log scale (#695)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import dash_mantine_components as dmc
import pandas as pd
import pytest

from dlboard.conftest import props
from dlboard.plugins.charts.line_chart import LineChart, LineChartSettings

if TYPE_CHECKING:
    from collections.abc import Sequence


def _rendered(values: Sequence[float], **settings: Any) -> dict[str, Any]:  # noqa: ANN401
    frame = pd.DataFrame({"run_id": 7, "step": range(len(values)), "loss": list(values)})
    chart = LineChart.render(LineChartSettings(column="loss", x_axis="step", **settings), frame)
    return props(next(node for node in chart._traverse() if isinstance(node, dmc.LineChart)))


def _texts(values: Sequence[float], **settings: Any) -> list[str]:  # noqa: ANN401
    frame = pd.DataFrame({"run_id": 7, "step": range(len(values)), "loss": list(values)})
    chart = LineChart.render(LineChartSettings(column="loss", x_axis="step", **settings), frame)
    return [str(node.children) for node in chart._traverse() if isinstance(node, dmc.Text)]


def _tensorboard(values: Sequence[float], weight: float) -> list[float]:
    """TensorBoard's smoothing, as its frontend computes it: a moving average, debiased for its first points."""
    last, smoothed = 0.0, list[float]()
    for count, value in enumerate(values, start=1):
        last = last * weight + (1 - weight) * value
        smoothed.append(last / (1 - weight**count))
    return smoothed


def test_a_smoothed_line_is_tensorboards_and_the_tooltip_names_what_was_logged_beside_it() -> None:
    values = [4.0, 1.0, 3.0, 0.5, 2.0, 0.25]

    rendered = _rendered(values, smoothing=0.6, sample=False)

    assert [row["7"] for row in rendered["data"]] == pytest.approx(_tensorboard(values, 0.6))
    assert [row["7__raw__y"] for row in rendered["data"]] == values
    assert [series["name"] for series in rendered["series"]] == ["7"]


def test_smoothing_is_off_by_default() -> None:
    rendered = _rendered([1.0, 2.0, 3.0])

    assert [row["7"] for row in rendered["data"]] == [1.0, 2.0, 3.0]
    assert "7__raw__y" not in rendered["data"][0]


def test_a_log_scale_leaves_out_what_it_cannot_place_and_says_how_much() -> None:
    rendered = _rendered([0.0, -1.0, 10.0, 100.0], y_scale="log", sample=False)

    assert [row["step"] for row in rendered["data"]] == [2, 3]
    assert rendered["yAxisProps"]["scale"] == "log"
    assert any(
        "2 values at or below 0 left out" in text for text in _texts([0.0, -1.0, 10.0, 100.0], y_scale="log")
    )


def test_a_log_scale_of_nothing_it_can_place_says_so_rather_than_drawing_nothing() -> None:
    texts = _texts([0.0, -1.0], y_scale="log")

    assert any("left out" in text for text in texts)
