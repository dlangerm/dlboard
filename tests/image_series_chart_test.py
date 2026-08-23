# pyright: reportPrivateUsage=false
"""Tests for the paginated image-series chart's render/hint logic."""

from __future__ import annotations

from typing import Any, cast

import pandas as pd
import pytest

from dltrack.plugins.charts.image_series import PAGE_SIZE, ImageChart, ImageChartSettings


def _artifacts_df(n_runs: int, steps_per_run: int = 1) -> pd.DataFrame:
    rows = [
        {"run_id": run_id, "step": step, "img": f"ref://{run_id}-{step}", "img__tags": {"run": str(run_id)}}
        for run_id in range(1, n_runs + 1)
        for step in range(steps_per_run)
    ]
    return pd.DataFrame(rows)


def _props(component: object) -> dict[str, Any]:
    # dash-mantine-components ships no py.typed marker, so pyright can't see these attrs.
    return cast("Any", component).to_plotly_json()["props"]


def test_render_reports_missing_key() -> None:
    stack = ImageChart.render(ImageChartSettings(key="missing"), pd.DataFrame({"run_id": [1], "step": [0]}))
    text = _props(_props(stack)["children"][0])["children"]
    assert "No artifacts logged" in text


def test_render_paginates_runs_into_groups_of_page_size() -> None:
    df = _artifacts_df(n_runs=PAGE_SIZE + 1)
    stack = ImageChart.render(ImageChartSettings(key="img"), df)
    children = _props(stack)["children"]
    grids = [c for c in children if _props(c).get("cols") is not None]
    assert len(grids) == 2
    assert len(_props(grids[0])["children"]) == PAGE_SIZE
    assert len(_props(grids[1])["children"]) == 1


def test_render_single_page_has_no_pager() -> None:
    df = _artifacts_df(n_runs=2)
    stack = ImageChart.render(ImageChartSettings(key="img"), df)
    children = _props(stack)["children"]
    # last child is the pager slot; with one page it's an empty html.Div (no "total" prop)
    assert _props(children[-1]).get("total") is None


@pytest.mark.parametrize(
    ("x_axis", "expected"),
    [("step", set[str]()), ("epoch", {"epoch"})],
)
def test_hint_required_columns(x_axis: str, expected: set[str]) -> None:
    assert ImageChart.hint_required_columns(ImageChartSettings(key="img", x_axis=x_axis)) == expected


def test_hint_required_artifact_keys() -> None:
    assert ImageChart.hint_required_artifact_keys(ImageChartSettings(key="img")) == {"img"}
