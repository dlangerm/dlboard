"""Tests for auto-generating chart panels from metric/artifact key naming conventions."""

from __future__ import annotations

import typing

import pytest

from dlboard.models import FILE_KIND_TAG, Artifact
from dlboard.models._view import ChartInstance, ColumnKind, PanelInstance
from dlboard.plugins.charts.bar_chart import BarChart
from dlboard.plugins.charts.file_list import FileListChart
from dlboard.plugins.charts.image_series import ImageChart
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve._pages._experiment import _chart_autogen as autogen
from dlboard.serve._pages._experiment._dataframe_helpers import ColumnCatalog

LineChart.register(allow_override=True)
ImageChart.register(allow_override=True)
BarChart.register(allow_override=True)
FileListChart.register(allow_override=True)


def _panel(name: str, *charts: ChartInstance[object, object]) -> PanelInstance[object, object]:
    return PanelInstance[object, object](name=name, charts=list(charts))


def _line(column: str, x_axis: str = "step") -> ChartInstance[object, object]:
    return ChartInstance[object, object](chart_type="line", parameters={"column": column, "x_axis": x_axis})


def _image(key: str) -> ChartInstance[object, object]:
    return ChartInstance[object, object](chart_type="image", parameters={"key": key})


# ---- split_group_name ----


def test_split_group_name_prefix() -> None:
    assert autogen.split_group_name("train/loss/mean", "/", "prefix") == "train"


def test_split_group_name_suffix() -> None:
    assert autogen.split_group_name("train/loss/mean", "/", "suffix") == "mean"


def test_split_group_name_underscore_delimiter() -> None:
    assert autogen.split_group_name("val_accuracy_top1", "_", "prefix") == "val"
    assert autogen.split_group_name("val_accuracy_top1", "_", "suffix") == "top1"


def test_split_group_name_groups_undelimited_keys_together() -> None:
    """A key with no delimiter to split on shouldn't get its own single-chart panel."""
    assert autogen.split_group_name("loss", "/", "prefix") == autogen.UNGROUPED_GROUP_NAME
    assert autogen.split_group_name("loss", "/", "suffix") == autogen.UNGROUPED_GROUP_NAME
    assert autogen.split_group_name("accuracy", "/", "prefix") == autogen.split_group_name(
        "loss", "/", "prefix"
    )


def test_split_group_name_empty_delimiter_groups_everything_together() -> None:
    assert autogen.split_group_name("train/loss", "", "prefix") == autogen.UNGROUPED_GROUP_NAME
    assert autogen.split_group_name("val/acc", "", "prefix") == autogen.UNGROUPED_GROUP_NAME


def test_split_group_name_ignores_leading_and_trailing_delimiters() -> None:
    assert autogen.split_group_name("/train/loss/", "/", "prefix") == "train"
    assert autogen.split_group_name("/train/loss/", "/", "suffix") == "loss"


# ---- lightning_granularity ----


def test_lightning_granularity_detects_step_and_epoch_suffixes() -> None:
    assert autogen.lightning_granularity("train_loss_epoch") == "epoch"
    assert autogen.lightning_granularity("train_loss_step") == "step"


def test_lightning_granularity_none_when_no_suffix() -> None:
    assert autogen.lightning_granularity("train_loss") is None


def test_lightning_granularity_none_when_suffix_is_the_whole_key() -> None:
    """A bare `_step`/`_epoch` key isn't a suffixed metric name -- there's no base name left."""
    assert autogen.lightning_granularity("_step") is None
    assert autogen.lightning_granularity("_epoch") is None


# ---- panel_name_for_group ----


def test_panel_name_for_group_metric_is_unchanged() -> None:
    assert autogen.panel_name_for_group("train", ColumnKind.METRIC) == "train"


def test_panel_name_for_group_artifact_gets_disambiguating_suffix() -> None:
    assert (
        autogen.panel_name_for_group("train", ColumnKind.ARTIFACT) == f"train{autogen.ARTIFACT_PANEL_SUFFIX}"
    )
    assert autogen.panel_name_for_group("train", ColumnKind.ARTIFACT) != autogen.panel_name_for_group(
        "train", ColumnKind.METRIC
    )


def test_panel_name_for_group_granularity_splits_the_panel() -> None:
    assert autogen.panel_name_for_group("train", ColumnKind.METRIC, granularity="epoch") == "train (epoch)"
    assert autogen.panel_name_for_group("train", ColumnKind.METRIC, granularity="step") == "train (step)"
    assert autogen.panel_name_for_group("train", ColumnKind.METRIC, granularity=None) == "train"


# ---- build_auto_panels ----


def test_build_auto_panels_groups_metrics_by_prefix() -> None:
    catalog = ColumnCatalog(metrics=("train/loss", "train/acc", "val/loss"))
    panels = autogen.build_auto_panels(catalog, delimiter="/", mode="prefix")

    by_name = {p.name: p for p in panels}
    assert set(by_name) == {"train", "val"}
    assert {c.parameters["column"] for c in by_name["train"].charts} == {"train/loss", "train/acc"}
    assert {c.parameters["column"] for c in by_name["val"].charts} == {"val/loss"}


def test_build_auto_panels_groups_metrics_by_suffix() -> None:
    catalog = ColumnCatalog(metrics=("train/loss", "val/loss", "val/acc"))
    panels = autogen.build_auto_panels(catalog, delimiter="/", mode="suffix")

    by_name = {p.name: p for p in panels}
    assert set(by_name) == {"loss", "acc"}
    assert {c.parameters["column"] for c in by_name["loss"].charts} == {"train/loss", "val/loss"}


def test_build_auto_panels_groups_undelimited_metrics_into_one_shared_panel() -> None:
    catalog = ColumnCatalog(metrics=("loss", "accuracy", "train/lr"))
    panels = autogen.build_auto_panels(catalog, delimiter="/", mode="prefix")

    by_name = {p.name: p for p in panels}
    assert set(by_name) == {autogen.UNGROUPED_GROUP_NAME, "train"}
    assert {c.parameters["column"] for c in by_name[autogen.UNGROUPED_GROUP_NAME].charts} == {
        "loss",
        "accuracy",
    }
    assert {c.parameters["column"] for c in by_name["train"].charts} == {"train/lr"}


def test_build_auto_panels_keeps_artifacts_in_their_own_panel_even_on_name_collision() -> None:
    """A metric group and an artifact group sharing a name must never merge into one panel."""
    catalog = ColumnCatalog(metrics=("train/loss",), artifact_chart_types={"train/sample_image": "image"})
    panels = autogen.build_auto_panels(catalog, delimiter="/", mode="prefix")

    by_name = {p.name: p for p in panels}
    assert set(by_name) == {"train", f"train{autogen.ARTIFACT_PANEL_SUFFIX}"}
    assert by_name["train"].charts[0].chart_type == "line"
    assert by_name[f"train{autogen.ARTIFACT_PANEL_SUFFIX}"].charts[0].chart_type == "image"


def test_build_auto_panels_default_chart_params() -> None:
    catalog = ColumnCatalog(metrics=("loss",), artifact_chart_types={"img": "image"})
    panels = autogen.build_auto_panels(catalog, delimiter="/", mode="prefix")

    metric_chart = next(c for p in panels for c in p.charts if c.chart_type == "line")
    assert metric_chart.parameters == {"column": "loss", "x_axis": "step"}

    artifact_chart = next(c for p in panels for c in p.charts if c.chart_type == "image")
    assert artifact_chart.parameters == {"key": "img"}


def test_build_auto_panels_lightning_splits_step_and_epoch_variants_into_adjacent_panels() -> None:
    catalog = ColumnCatalog(metrics=("train/loss_step", "train/loss_epoch", "train/lr"))
    panels = autogen.build_auto_panels(catalog, delimiter="/", mode="prefix", lightning=True)

    by_name = {p.name: p for p in panels}
    assert set(by_name) == {"train (step)", "train (epoch)", "train"}
    assert {c.parameters["column"] for c in by_name["train (step)"].charts} == {"train/loss_step"}
    assert {c.parameters["column"] for c in by_name["train (epoch)"].charts} == {"train/loss_epoch"}
    assert {c.parameters["column"] for c in by_name["train"].charts} == {"train/lr"}


def test_build_auto_panels_ignores_lightning_suffixes_when_not_lightning() -> None:
    catalog = ColumnCatalog(metrics=("train/loss_step", "train/loss_epoch"))
    panels = autogen.build_auto_panels(catalog, delimiter="/", mode="prefix", lightning=False)

    assert [p.name for p in panels] == ["train"]
    assert {c.parameters["column"] for c in panels[0].charts} == {"train/loss_step", "train/loss_epoch"}


def test_build_auto_panels_empty_catalog_produces_no_panels() -> None:
    assert autogen.build_auto_panels(ColumnCatalog(), delimiter="/", mode="prefix") == []


# ---- find_uncharted_keys ----


def test_find_uncharted_keys_excludes_already_charted_metrics_and_artifacts() -> None:
    panels = [_panel("train", _line("train/loss")), _panel("imgs", _image("train/sample"))]
    catalog = ColumnCatalog(
        metrics=("train/loss", "train/acc"),
        artifact_chart_types={"train/sample": "image", "train/other_img": "image"},
    )

    uncharted = autogen.find_uncharted_keys(panels, catalog)

    assert uncharted == autogen.UnchartedKeys(metrics=["train/acc"], artifacts=["train/other_img"])


def test_find_uncharted_keys_all_uncharted_when_no_panels() -> None:
    catalog = ColumnCatalog(metrics=("loss",), artifact_chart_types={"img": "image"})
    assert autogen.find_uncharted_keys([], catalog) == autogen.UnchartedKeys(
        metrics=["loss"], artifacts=["img"]
    )


def test_find_uncharted_keys_nothing_uncharted_when_fully_covered() -> None:
    panels = [_panel("p", _line("loss"), _image("img"))]
    catalog = ColumnCatalog(metrics=("loss",), artifact_chart_types={"img": "image"})
    assert autogen.find_uncharted_keys(panels, catalog) == autogen.UnchartedKeys(metrics=[], artifacts=[])


def test_files_in_one_directory_share_one_chart_listing_the_directory() -> None:
    catalog = ColumnCatalog(
        artifact_chart_types={
            "checkpoints/a.ckpt": "files",
            "checkpoints/b.ckpt": "files",
            "model.onnx": "files",
            "samples/x.png": "image",
            "samples/y.png": "image",
        }
    )

    by_name = {p.name: p for p in autogen.build_auto_panels(catalog, delimiter="/", mode="prefix")}

    assert [(c.chart_type, c.parameters) for c in by_name["checkpoints (artifacts)"].charts] == [
        ("files", {"key_prefix": "checkpoints/"})
    ]
    assert [(c.chart_type, c.parameters) for c in by_name["Ungrouped (artifacts)"].charts] == [
        ("files", {"key": "model.onnx"})
    ]
    assert len(by_name["samples (artifacts)"].charts) == 2


def test_the_auto_generate_preview_counts_a_directory_of_files_as_one_chart() -> None:
    catalog = ColumnCatalog(artifact_chart_types={f"checkpoints/{n}.ckpt": "files" for n in range(40)})

    groups = autogen.group_keys_into_panels(catalog, delimiter="/", mode="prefix")

    assert groups == {"checkpoints (artifacts)": [("checkpoints/", ColumnKind.ARTIFACT)]}


def test_keys_under_a_charted_prefix_are_not_suggested_again() -> None:
    charted = ChartInstance[object, object](chart_type="files", parameters={"key_prefix": "checkpoints/"})
    catalog = ColumnCatalog(
        artifact_chart_types={"checkpoints/a.ckpt": "files", "other/b.ckpt": "files", "other/c.ckpt": "files"}
    )

    uncharted = autogen.find_uncharted_keys([_panel("p", charted)], catalog)
    suggestions = autogen.build_suggestions(uncharted, catalog, delimiter="/", mode="prefix")

    assert uncharted.artifacts == ["other/b.ckpt", "other/c.ckpt"]
    assert [(s.key, s.chart.parameters) for s in suggestions] == [("other/", {"key_prefix": "other/"})]


# ---- build_suggestions ----


def test_build_suggestions_pairs_each_key_with_its_default_chart_and_target_panel() -> None:
    uncharted = autogen.UnchartedKeys(metrics=["train/acc"], artifacts=["train/sample"])

    catalog = ColumnCatalog(artifact_chart_types={"train/sample": "image"})

    suggestions = autogen.build_suggestions(uncharted, catalog, delimiter="/", mode="prefix")

    assert suggestions == [
        autogen.Suggestion(
            key="train/acc",
            kind=ColumnKind.METRIC,
            panel_name="train",
            chart=ChartInstance[object, object](
                chart_type="line", parameters={"column": "train/acc", "x_axis": "step"}
            ),
        ),
        autogen.Suggestion(
            key="train/sample",
            kind=ColumnKind.ARTIFACT,
            panel_name=f"train{autogen.ARTIFACT_PANEL_SUFFIX}",
            chart=ChartInstance[object, object](chart_type="image", parameters={"key": "train/sample"}),
        ),
    ]


def test_build_suggestions_single_value_metric_gets_a_bar_chart_across_runs() -> None:
    uncharted = autogen.UnchartedKeys(metrics=["final_accuracy"], artifacts=[])

    suggestions = autogen.build_suggestions(
        uncharted,
        ColumnCatalog(single_value_metrics=frozenset({"final_accuracy"})),
        delimiter="/",
        mode="prefix",
    )

    assert suggestions == [
        autogen.Suggestion(
            key="final_accuracy",
            kind=ColumnKind.METRIC,
            panel_name=autogen.UNGROUPED_GROUP_NAME,
            chart=ChartInstance[object, object](
                chart_type="bar", parameters={"column": "final_accuracy", "x_axis": "run_id"}
            ),
        )
    ]


def test_build_auto_panels_single_value_metric_gets_a_bar_chart() -> None:
    catalog = ColumnCatalog(metrics=("final_accuracy",), single_value_metrics=frozenset({"final_accuracy"}))
    panels = autogen.build_auto_panels(catalog, delimiter="/", mode="prefix")
    assert panels[0].charts[0].chart_type == "bar"


def test_build_suggestions_lightning_splits_panel_by_granularity() -> None:
    uncharted = autogen.UnchartedKeys(metrics=["train/loss_epoch"], artifacts=[])

    suggestions = autogen.build_suggestions(
        uncharted, ColumnCatalog(), delimiter="/", mode="prefix", lightning=True
    )

    assert suggestions == [
        autogen.Suggestion(
            key="train/loss_epoch",
            kind=ColumnKind.METRIC,
            panel_name="train (epoch)",
            chart=ChartInstance[object, object](
                chart_type="line", parameters={"column": "train/loss_epoch", "x_axis": "step"}
            ),
        )
    ]


def _artifact(fname: str, **tags: str) -> Artifact:
    return Artifact(key="k", fname=fname, run_id=1, experiment_id=1, step=0, ref="r://a", tags=tags)


@pytest.mark.parametrize(
    ("artifact", "chart_type"),
    [
        pytest.param(_artifact("a.png"), "image", id="an-image"),
        pytest.param(_artifact("last.ckpt", **{FILE_KIND_TAG: "checkpoint"}), "files", id="a-checkpoint"),
        pytest.param(
            _artifact("a.png", **{FILE_KIND_TAG: "checkpoint"}), "files", id="a-file-named-like-an-image"
        ),
        pytest.param(_artifact("grads.npy"), None, id="something-no-chart-type-claims"),
    ],
)
def test_an_artifact_gets_the_chart_type_that_says_it_can_display_it(
    artifact: Artifact, chart_type: str | None
) -> None:
    assert autogen.default_artifact_chart(artifact) == chart_type


def test_an_artifact_key_no_chart_type_can_display_gets_no_generated_chart() -> None:
    """Rather than a chart that cannot show it -- there are more kinds of artifact than images and files."""
    catalog = ColumnCatalog(artifacts=("grads",))

    assert not catalog.has_chartable_keys
    assert autogen.build_auto_panels(catalog, delimiter="/", mode="prefix") == []
    assert autogen.find_uncharted_keys([], catalog) == autogen.UnchartedKeys(metrics=[], artifacts=[])


def test_each_artifact_key_is_charted_as_the_catalog_says() -> None:
    catalog = ColumnCatalog(artifact_chart_types={"ckpt": "files", "img": "image"})

    auto = {
        c.parameters["key"]: c.chart_type
        for p in autogen.build_auto_panels(catalog, delimiter="", mode="prefix")
        for c in p.charts
    }
    suggested = {
        s.key: s.chart.chart_type
        for s in autogen.build_suggestions(
            autogen.UnchartedKeys(metrics=[], artifacts=["ckpt", "img"]), catalog, delimiter="", mode="prefix"
        )
    }

    assert auto == suggested == {"ckpt": "files", "img": "image"}


def _panel_of(name: str, n_charts: int) -> PanelInstance[typing.Any, typing.Any]:
    return _panel(name, *[_line(f"{name}{i}") for i in range(n_charts)])


@pytest.mark.parametrize(
    ("sizes", "expected"),
    [
        pytest.param([], [], id="no-panels"),
        pytest.param([("a", 3), ("b", 3)], ["a"], id="the-first-panel-if-it-is-small"),
        pytest.param([("a", autogen.AUTO_OPEN_MAX_CHARTS)], ["a"], id="exactly-at-the-limit"),
        pytest.param([("a", 126), ("b", 4), ("c", 2)], ["b"], id="skips-a-huge-first-panel"),
        pytest.param([("a", 126), ("b", 40)], [], id="nothing-small-enough-so-nothing-opens"),
    ],
)
def test_auto_generate_only_opens_a_panel_small_enough_to_render_at_once(
    sizes: list[tuple[str, int]], expected: list[str]
) -> None:
    assert autogen.panel_to_open([_panel_of(name, n) for name, n in sizes]) == expected


def _suggestion(key: str, panel_name: str, kind: ColumnKind = ColumnKind.METRIC) -> autogen.Suggestion:
    return autogen.Suggestion(key=key, kind=kind, panel_name=panel_name, chart=_line(key))


_SUGGESTIONS = [
    _suggestion("val/loss", "val"),
    _suggestion("train/loss", "train"),
    _suggestion("train/acc", "train"),
    _suggestion("img", "samples (artifacts)", ColumnKind.ARTIFACT),
    _suggestion("aaa/last", "zz"),
]


@pytest.mark.parametrize(
    ("text", "sort", "expected"),
    [
        pytest.param(
            "",
            autogen.SuggestSort.NAME,
            ["aaa/last", "img", "train/acc", "train/loss", "val/loss"],
            id="by-name",
        ),
        pytest.param(
            "",
            autogen.SuggestSort.PANEL,
            ["img", "train/acc", "train/loss", "val/loss", "aaa/last"],
            id="by-panel",
        ),
        pytest.param("LOSS", autogen.SuggestSort.NAME, ["train/loss", "val/loss"], id="key-ignoring-case"),
        pytest.param(
            "  train ", autogen.SuggestSort.NAME, ["train/acc", "train/loss"], id="trimmed-panel-name"
        ),
        pytest.param("nothing like this", autogen.SuggestSort.NAME, [], id="no-match"),
    ],
)
def test_visible_suggestions_filters_by_key_or_panel_and_sorts(
    text: str, sort: autogen.SuggestSort, expected: list[str]
) -> None:
    assert [s.key for s in autogen.visible_suggestions(_SUGGESTIONS, text=text, sort=sort)] == expected


def test_a_suggestions_checkbox_value_round_trips_even_when_the_key_has_a_colon() -> None:
    picked = [_suggestion("val/loss", "val"), _suggestion("a:b", "p", ColumnKind.ARTIFACT)]

    assert autogen.parse_selection(s.selection_value for s in picked) == {
        (ColumnKind.METRIC, "val/loss"),
        (ColumnKind.ARTIFACT, "a:b"),
    }
