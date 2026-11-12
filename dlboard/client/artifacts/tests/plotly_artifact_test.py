"""Tests for the Plotly artifacts: a figure, a template's bindings, and the data streamed into one."""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

import numpy as np
import plotly.graph_objects as go
import pydantic
import pytest
import torch

from dlboard.client.artifacts.plotly import BINDINGS_MEMBER, MAX_DATA_VALUES, PlotlyData, PlotlyFigure
from dlboard.models import PLOTLY_DATA_EXTENSION, PLOTLY_FIGURE_EXTENSION, plotly_data_key

if TYPE_CHECKING:
    from pathlib import Path


def _written(artifact: PlotlyFigure | PlotlyData, tmp_path: Path) -> tuple[Any, Any]:
    logged, target = artifact.to_artifact(tmp_path, run_id=1, experiment_id=2)
    assert logged.fname == target.name
    assert (logged.run_id, logged.experiment_id) == (1, 2)
    return logged, json.loads(target.read_text())


@pytest.mark.parametrize(
    "figure",
    [
        go.Figure(go.Scatter(x=np.array([1.0, 2.0, 3.0]), y=[3, 1, 2], name="curve"), layout={"title": "pr"}),
        {
            "data": [{"type": "scatter", "x": np.array([1.0, 2.0, 3.0]), "y": [3, 1, 2], "name": "curve"}],
            "layout": {"title": "pr"},
        },
    ],
    ids=["plotly-figure", "mapping"],
)
def test_a_figure_is_written_as_plotly_json_with_its_metadata(tmp_path: Path, figure: Any) -> None:  # noqa: ANN401
    logged, written = _written(
        PlotlyFigure.from_figure(figure, "eval/pr", step=3, tags={"split": "val"}), tmp_path
    )

    assert logged.fname.endswith(PLOTLY_FIGURE_EXTENSION)
    assert (logged.key, logged.step, logged.tags) == ("eval/pr", 3, {"split": "val"})
    assert written["data"][0]["name"] == "curve"
    assert BINDINGS_MEMBER not in written


def test_a_figure_logged_once_is_at_step_zero() -> None:
    assert PlotlyFigure.from_figure({"data": []}, "class_balance").step == 0


def test_nan_and_infinities_are_gaps_not_invalid_json(tmp_path: Path) -> None:
    figure = {"data": [{"y": np.array([1.0, math.nan, math.inf]), "x": [math.nan, 1, 2]}]}

    _logged, written = _written(PlotlyFigure.from_figure(figure, "gaps"), tmp_path)

    assert written["data"][0] == {"y": [1.0, None, None], "x": [None, 1, 2]}


def test_a_template_keeps_its_bindings_beside_the_figure(tmp_path: Path) -> None:
    figure = go.Figure([go.Scattermap(mode="lines"), go.Heatmap(colorscale="Viridis")])

    template = PlotlyFigure.from_figure(
        figure, "gps", bind={0: {"lat": "lat", "lon": "lon"}, 1: {"z": "confusion"}}
    )

    _logged, written = _written(template, tmp_path)
    assert written[BINDINGS_MEMBER] == {"0": {"lat": "lat", "lon": "lon"}, "1": {"z": "confusion"}}
    assert [trace["type"] for trace in written["data"]] == ["scattermap", "heatmap"]


@pytest.mark.parametrize(
    "figure",
    [{"layout": {}}, {"data": "not traces"}],
    ids=["no-traces", "traces-not-a-list"],
)
def test_what_is_not_a_figure_is_rejected(figure: dict[str, Any]) -> None:
    with pytest.raises(pydantic.ValidationError):
        PlotlyFigure.from_figure(figure, "bad")


@pytest.mark.parametrize(
    "bind",
    [{1: {"x": "x"}}, {0: {}}, {0: {"marker..color": "c"}}, {0: {"x": ""}}, {-1: {"x": "x"}}],
    ids=["trace-out-of-range", "nothing-bound", "bad-attribute-path", "empty-column", "negative-trace"],
)
def test_bindings_that_cannot_be_drawn_are_rejected(bind: dict[int, dict[str, str]]) -> None:
    with pytest.raises(pydantic.ValidationError):
        PlotlyFigure.from_figure({"data": [{"type": "scatter"}]}, "bad", bind=bind)


def test_a_nested_attribute_can_be_bound() -> None:
    template = PlotlyFigure.from_figure(
        {"data": [{"type": "scatter"}]}, "t", bind={0: {"marker.color": "step"}}
    )

    assert json.loads(template.data)[BINDINGS_MEMBER] == {"0": {"marker.color": "step"}}


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (0.5, [0.5]),
        ([1, 2, 3], [1, 2, 3]),
        (np.array([[1.0, math.nan], [3.0, 4.0]]), [[1.0, None], [3.0, 4.0]]),
        (torch.tensor([1.5, 2.5], dtype=torch.bfloat16), [1.5, 2.5]),
        (["cat", "dog"], ["cat", "dog"]),
    ],
    ids=["scalar", "list", "2-d-with-nan", "bfloat16-tensor", "strings"],
)
def test_data_columns_are_written_as_plain_values(tmp_path: Path, values: Any, expected: list[Any]) -> None:  # noqa: ANN401
    logged, written = _written(PlotlyData(template="gps", step=7, columns={"c": values}), tmp_path)

    assert logged.fname.endswith(PLOTLY_DATA_EXTENSION)
    assert (logged.key, logged.step) == (plotly_data_key("gps"), 7)
    assert written == {"columns": {"c": expected}}


@pytest.mark.parametrize(
    "columns",
    [
        {},
        {"c": np.zeros((2, 2, 2))},
        {"c": [[1, 2], [3]]},
        {"a": np.zeros(MAX_DATA_VALUES), "b": [1]},
    ],
    ids=["no-columns", "3-d", "ragged", "too-many-values"],
)
def test_data_that_cannot_be_drawn_is_rejected(columns: dict[str, Any]) -> None:
    with pytest.raises(pydantic.ValidationError):
        PlotlyData(template="gps", step=0, columns=columns)
