"""A chart whose shown runs logged nothing it draws renders an empty chart, not an error."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from dlboard.models._view import ChartInstance
from dlboard.plugins.charts.bar_chart import BarChart
from dlboard.plugins.charts.file_list import FileListChart
from dlboard.plugins.charts.histogram import HistogramChart
from dlboard.plugins.charts.image_series import ImageChart
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.series_band import SeriesBandChart
from dlboard.plugins.charts.table_chart import TableChart

for _chart_type in (
    LineChart,
    BarChart,
    HistogramChart,
    FileListChart,
    ImageChart,
    TableChart,
    SeriesBandChart,
):
    _chart_type.register(allow_override=True)


@pytest.mark.parametrize(
    ("chart_type", "parameters"),
    [
        pytest.param(LineChart.name, {"column": "val/loss", "x_axis": "step"}, id="line"),
        pytest.param(BarChart.name, {"column": "val/loss", "x_axis": "step"}, id="bar"),
        pytest.param(HistogramChart.name, {"key": "grad"}, id="histogram"),
        pytest.param(FileListChart.name, {"key": "files"}, id="file_list"),
        pytest.param(ImageChart.name, {"key": "img"}, id="image"),
        pytest.param(TableChart.name, {}, id="table"),
        pytest.param(SeriesBandChart.name, {"metrics": ["val/loss"]}, id="series_band"),
    ],
)
def test_every_chart_type_renders_a_frame_with_no_rows_and_no_columns(
    chart_type: str, parameters: dict[str, Any]
) -> None:
    # What `PanelData.for_chart` hands a chart when none of the shown runs logged what it draws.
    chart = ChartInstance[pd.DataFrame, object](chart_type=chart_type, parameters=parameters)

    chart.render(pd.DataFrame())
