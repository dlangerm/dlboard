# pyright: reportPrivateUsage=false
"""Tests for the paginated image-series chart's render/hint logic."""

from __future__ import annotations

from typing import Any, cast

import pandas as pd
import pytest

from dltrack.plugins.charts.image_series import (
    MAX_SLIDER_LABELS,
    PAGE_SIZE,
    ImageChart,
    ImageChartSettings,
    _slider_marks,
)


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


def _captions_store_data(stack: object) -> dict[str, Any]:
    # dcc.Store is always the first child of the returned Stack.
    return _props(_props(stack)["children"][0])["data"]["per_run_captions"]


def test_render_formats_tags_dict_into_captions() -> None:
    """`build_artifacts_dataframe` puts `Artifact.tags` dicts straight into the dataframe (not JSON
    strings) — captions must be built from those dicts directly, not `json.loads`-parsed.
    """
    df = _artifacts_df(n_runs=1)  # tags column holds {"run": "1"}, a dict, not a JSON string
    stack = ImageChart.render(ImageChartSettings(key="img"), df)

    assert _captions_store_data(stack) == {"1": {"0": "run: 1"}}


def test_render_handles_missing_tags_for_some_rows_without_crashing() -> None:
    df = pd.DataFrame(
        [
            {"run_id": 1, "step": 0, "img": "ref://1-0", "img__tags": {"split": "train"}},
            {"run_id": 1, "step": 1, "img": "ref://1-1", "img__tags": None},
        ]
    )
    stack = ImageChart.render(ImageChartSettings(key="img"), df)

    assert _captions_store_data(stack) == {"1": {"0": "split: train", "1": ""}}


# ---- _slider_marks: every real step is a snap target, but only a sparse subset gets a label ----


def test_slider_marks_labels_every_step_when_few() -> None:
    marks = _slider_marks(list(range(5)))
    assert [m["value"] for m in marks] == [0, 1, 2, 3, 4]
    assert all("label" in m for m in marks)


def test_slider_marks_has_a_mark_for_every_step_even_when_many() -> None:
    steps = list(range(200))
    marks = _slider_marks(steps, max_labels=10)

    assert [m["value"] for m in marks] == steps

    labeled = [m for m in marks if "label" in m]
    assert len(labeled) <= 12  # ~max_labels, plus the guaranteed-included last step
    assert marks[-1]["label"] == "199"  # last step is always labeled


def _slider_props(stack: object) -> dict[str, Any]:
    # dcc.Store is child 0; dmc.Slider is child 1.
    return _props(_props(stack)["children"][1])


def test_render_slider_snaps_only_to_real_steps_and_has_bottom_margin() -> None:
    """Regression: with sparse steps, the slider used to allow continuous dragging between real
    step values, so moving it didn't always change what was shown. `restrictToMarks` fixes that.
    Also pins the added bottom margin, needed so many wrapped mark labels don't run into the
    "Run N" text of the row below.
    """
    df = _artifacts_df(n_runs=1, steps_per_run=1)
    stack = ImageChart.render(ImageChartSettings(key="img"), df)

    slider = _slider_props(stack)
    assert slider["restrictToMarks"] is True
    assert slider["mb"] == "xl"


def test_render_slider_marks_match_max_slider_labels_constant() -> None:
    df = pd.DataFrame([{"run_id": 1, "step": s, "img": f"ref://{s}"} for s in range(200)])
    stack = ImageChart.render(ImageChartSettings(key="img"), df)

    slider = _slider_props(stack)
    labeled = [m for m in slider["marks"] if "label" in m]
    assert len(labeled) <= MAX_SLIDER_LABELS + 2
