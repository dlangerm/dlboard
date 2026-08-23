# pyright: reportPrivateUsage=false
"""Tests for auto-generating chart panels from metric/artifact key naming conventions."""

from __future__ import annotations

from dltrack.models._view import ChartInstance, ColumnKind, PanelInstance
from dltrack.plugins.charts.image_series import ImageChart
from dltrack.plugins.charts.line_chart import LineChart
from dltrack.plugins.pages._chart_autogen import (
    ARTIFACT_PANEL_SUFFIX,
    UNGROUPED_GROUP_NAME,
    Suggestion,
    UnchartedKeys,
    build_auto_panels,
    build_suggestions,
    chartable_metric_columns,
    find_uncharted_keys,
    panel_name_for_group,
    split_group_name,
)

LineChart.register(allow_override=True)
ImageChart.register(allow_override=True)


def _panel(name: str, *charts: ChartInstance[object, object]) -> PanelInstance[object, object]:
    return PanelInstance[object, object](name=name, charts=list(charts))


def _line(column: str, x_axis: str = "step") -> ChartInstance[object, object]:
    return ChartInstance[object, object](chart_type="line", parameters={"column": column, "x_axis": x_axis})


def _image(key: str) -> ChartInstance[object, object]:
    return ChartInstance[object, object](chart_type="image", parameters={"key": key})


# ---- split_group_name ----


def test_split_group_name_prefix() -> None:
    assert split_group_name("train/loss/mean", "/", "prefix") == "train"


def test_split_group_name_suffix() -> None:
    assert split_group_name("train/loss/mean", "/", "suffix") == "mean"


def test_split_group_name_underscore_delimiter() -> None:
    assert split_group_name("val_accuracy_top1", "_", "prefix") == "val"
    assert split_group_name("val_accuracy_top1", "_", "suffix") == "top1"


def test_split_group_name_groups_undelimited_keys_together() -> None:
    """A key with no delimiter to split on shouldn't get its own single-chart panel."""
    assert split_group_name("loss", "/", "prefix") == UNGROUPED_GROUP_NAME
    assert split_group_name("loss", "/", "suffix") == UNGROUPED_GROUP_NAME
    assert split_group_name("accuracy", "/", "prefix") == split_group_name("loss", "/", "prefix")


def test_split_group_name_empty_delimiter_groups_everything_together() -> None:
    assert split_group_name("train/loss", "", "prefix") == UNGROUPED_GROUP_NAME
    assert split_group_name("val/acc", "", "prefix") == UNGROUPED_GROUP_NAME


def test_split_group_name_ignores_leading_and_trailing_delimiters() -> None:
    assert split_group_name("/train/loss/", "/", "prefix") == "train"
    assert split_group_name("/train/loss/", "/", "suffix") == "loss"


# ---- panel_name_for_group ----


def test_panel_name_for_group_metric_is_unchanged() -> None:
    assert panel_name_for_group("train", ColumnKind.METRIC) == "train"


def test_panel_name_for_group_artifact_gets_disambiguating_suffix() -> None:
    assert panel_name_for_group("train", ColumnKind.ARTIFACT) == f"train{ARTIFACT_PANEL_SUFFIX}"
    assert panel_name_for_group("train", ColumnKind.ARTIFACT) != panel_name_for_group(
        "train", ColumnKind.METRIC
    )


# ---- chartable_metric_columns / artifact_keys exclusions ----


def test_chartable_metric_columns_excludes_bookkeeping_columns() -> None:
    column_kinds = {
        "train/loss": "metric",
        "step": "metric",
        "run_id": "metric",
        "timestamp_utc": "metric",
        "index": "metric",
        "experiment_id": "metric",
        "img": "artifact",
    }
    assert chartable_metric_columns(column_kinds) == ["train/loss"]


# ---- build_auto_panels ----


def test_build_auto_panels_groups_metrics_by_prefix() -> None:
    column_kinds = {
        "train/loss": "metric",
        "train/acc": "metric",
        "val/loss": "metric",
        "step": "metric",
    }
    panels = build_auto_panels(column_kinds, delimiter="/", mode="prefix")

    by_name = {p.name: p for p in panels}
    assert set(by_name) == {"train", "val"}
    assert {c.parameters["column"] for c in by_name["train"].charts} == {"train/loss", "train/acc"}
    assert {c.parameters["column"] for c in by_name["val"].charts} == {"val/loss"}


def test_build_auto_panels_groups_metrics_by_suffix() -> None:
    column_kinds = {"train/loss": "metric", "val/loss": "metric", "val/acc": "metric"}
    panels = build_auto_panels(column_kinds, delimiter="/", mode="suffix")

    by_name = {p.name: p for p in panels}
    assert set(by_name) == {"loss", "acc"}
    assert {c.parameters["column"] for c in by_name["loss"].charts} == {"train/loss", "val/loss"}


def test_build_auto_panels_groups_undelimited_metrics_into_one_shared_panel() -> None:
    column_kinds = {"loss": "metric", "accuracy": "metric", "train/lr": "metric"}
    panels = build_auto_panels(column_kinds, delimiter="/", mode="prefix")

    by_name = {p.name: p for p in panels}
    assert set(by_name) == {UNGROUPED_GROUP_NAME, "train"}
    assert {c.parameters["column"] for c in by_name[UNGROUPED_GROUP_NAME].charts} == {"loss", "accuracy"}
    assert {c.parameters["column"] for c in by_name["train"].charts} == {"train/lr"}


def test_build_auto_panels_keeps_artifacts_in_their_own_panel_even_on_name_collision() -> None:
    """A metric group and an artifact group sharing a name must never merge into one panel."""
    column_kinds = {
        "train/loss": "metric",
        "train/sample_image": "artifact",
    }
    panels = build_auto_panels(column_kinds, delimiter="/", mode="prefix")

    by_name = {p.name: p for p in panels}
    assert set(by_name) == {"train", f"train{ARTIFACT_PANEL_SUFFIX}"}
    assert by_name["train"].charts[0].chart_type == "line"
    assert by_name[f"train{ARTIFACT_PANEL_SUFFIX}"].charts[0].chart_type == "image"


def test_build_auto_panels_default_chart_params() -> None:
    column_kinds = {"loss": "metric", "img": "artifact"}
    panels = build_auto_panels(column_kinds, delimiter="/", mode="prefix")

    metric_chart = next(c for p in panels for c in p.charts if c.chart_type == "line")
    assert metric_chart.parameters == {"column": "loss", "x_axis": "step"}

    artifact_chart = next(c for p in panels for c in p.charts if c.chart_type == "image")
    assert artifact_chart.parameters == {"key": "img"}


def test_build_auto_panels_empty_column_kinds_produces_no_panels() -> None:
    assert build_auto_panels({}, delimiter="/", mode="prefix") == []


# ---- find_uncharted_keys ----


def test_find_uncharted_keys_excludes_already_charted_metrics_and_artifacts() -> None:
    panels = [_panel("train", _line("train/loss")), _panel("imgs", _image("train/sample"))]
    column_kinds = {
        "train/loss": "metric",
        "train/acc": "metric",
        "train/sample": "artifact",
        "train/other_img": "artifact",
    }

    uncharted = find_uncharted_keys(panels, column_kinds)

    assert uncharted == UnchartedKeys(metrics=["train/acc"], artifacts=["train/other_img"])


def test_find_uncharted_keys_all_uncharted_when_no_panels() -> None:
    column_kinds = {"loss": "metric", "img": "artifact"}
    assert find_uncharted_keys([], column_kinds) == UnchartedKeys(metrics=["loss"], artifacts=["img"])


def test_find_uncharted_keys_nothing_uncharted_when_fully_covered() -> None:
    panels = [_panel("p", _line("loss"), _image("img"))]
    column_kinds = {"loss": "metric", "img": "artifact"}
    assert find_uncharted_keys(panels, column_kinds) == UnchartedKeys(metrics=[], artifacts=[])


# ---- build_suggestions ----


def test_build_suggestions_pairs_each_key_with_its_default_chart_and_target_panel() -> None:
    uncharted = UnchartedKeys(metrics=["train/acc"], artifacts=["train/sample"])

    suggestions = build_suggestions(uncharted, delimiter="/", mode="prefix")

    assert suggestions == [
        Suggestion(
            key="train/acc",
            kind=ColumnKind.METRIC,
            panel_name="train",
            chart=ChartInstance[object, object](
                chart_type="line", parameters={"column": "train/acc", "x_axis": "step"}
            ),
        ),
        Suggestion(
            key="train/sample",
            kind=ColumnKind.ARTIFACT,
            panel_name=f"train{ARTIFACT_PANEL_SUFFIX}",
            chart=ChartInstance[object, object](chart_type="image", parameters={"key": "train/sample"}),
        ),
    ]
