"""Tests for the paginated image-series chart's render/hint logic."""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from dlboard.conftest import find_all_props
from dlboard.conftest import props as _props
from dlboard.models import (
    DEFAULT_RENDER_CONTEXT,
    LAYER_KIND_TAG,
    OVERLAY_KEY_SUFFIX,
    RUN_NAME_COLUMN,
    UNNAMED_OVERLAY,
    ChartInstance,
    ChartTypeRegistry,
    ColumnKind,
    DataRequest,
    LayerKind,
    ParameterFieldType,
    Quantity,
    RenderContext,
    TagDisplay,
    TimeUnit,
    artifact_column,
    artifact_fname_column,
    artifact_tags_column,
    overlay_key,
)
from dlboard.plugins.charts.image_series import (
    MAX_SLIDER_LABELS,
    ImageChart,
    ImageChartSettings,
    ImageViewerState,
    LayerStyle,
    LayerVisibility,
    ViewedLayer,
    _slider_marks,
)
from dlboard.plugins.charts.line_chart import LineChart

LineChart.register(allow_override=True)  # a chart type that keeps no viewer state


def _artifacts_df(n_runs: int, steps_per_run: int = 1) -> pd.DataFrame:
    rows = [
        {
            "run_id": run_id,
            "step": step,
            artifact_column("img"): f"ref://{run_id}-{step}",
            artifact_tags_column("img"): {"run": str(run_id)},
        }
        for run_id in range(1, n_runs + 1)
        for step in range(steps_per_run)
    ]
    return pd.DataFrame(rows)


def test_render_reports_missing_key() -> None:
    stack = ImageChart.render(ImageChartSettings(key="missing"), pd.DataFrame({"run_id": [1], "step": [0]}))
    text = _props(_props(stack)["children"][0])["children"]  # pyrefly: ignore [unknown-argument-type]
    assert "No artifacts logged" in text


@pytest.mark.parametrize("page_size", [2, 5])
def test_render_paginates_runs_into_groups_of_the_pages_size(page_size: int) -> None:
    df = _artifacts_df(n_runs=page_size + 1)
    stack = ImageChart.render_in(ImageChartSettings(key="img"), df, RenderContext(page_size=page_size))
    children = _props(stack)["children"]
    grids = [c for c in children if _props(c).get("cols") is not None]
    assert len(grids) == 2
    assert len(_props(grids[0])["children"]) == page_size
    assert len(_props(grids[1])["children"]) == 1


def test_render_single_page_has_no_pager() -> None:
    df = _artifacts_df(n_runs=2)
    stack = ImageChart.render(ImageChartSettings(key="img"), df)
    children = _props(stack)["children"]
    # last child is the pager slot; with one page it's an empty html.Div (no "total" prop)
    assert _props(children[-1]).get("total") is None  # pyrefly: ignore [unknown-argument-type]


@pytest.mark.parametrize(
    ("settings", "metrics", "artifacts"),
    [
        pytest.param(ImageChartSettings(key="img"), set[str](), {"img"}, id="by-step"),
        pytest.param(ImageChartSettings(key="img", x_axis="epoch"), {"epoch"}, {"img"}, id="by-a-metric"),
        pytest.param(
            ImageChartSettings(key="img", layers={"boxes": LayerStyle(), "mask": LayerStyle()}),
            set[str](),
            {"img", "boxes", "mask"},
            id="the-layers-are-charted-with-the-image",
        ),
    ],
)
def test_the_chart_reads_its_picture_its_layers_and_every_overlay_over_it(
    settings: ImageChartSettings, metrics: set[str], artifacts: set[str]
) -> None:
    assert ImageChart.data_request(settings) == DataRequest(
        metrics=frozenset(metrics),
        artifacts=frozenset(artifacts),
        artifact_prefixes=frozenset({overlay_key("img")}),
    )


def _captions_store_data(stack: object) -> dict[str, Any]:
    # dcc.Store is always the first child of the returned Stack.
    return _props(_props(stack)["children"][1])["data"][  # pyrefly: ignore [unknown-argument-type]
        "per_run_captions"
    ]


def test_render_formats_tags_dict_into_captions() -> None:
    """`build_artifacts_dataframe` puts `Artifact.tags` dicts straight into the dataframe (not JSON
    strings) — captions must be built from those dicts directly, not `json.loads`-parsed.
    """
    df = _artifacts_df(n_runs=1)  # tags column holds {"run": "1"}, a dict, not a JSON string
    stack = ImageChart.render(ImageChartSettings(key="img"), df)

    assert _captions_store_data(stack) == {"1": {"0": "run: 1"}}


def test_the_enlarged_views_tags_and_run_marks_come_with_the_chart_in_the_formats_its_caption_has() -> None:
    df = _artifacts_df(n_runs=2)
    data = _store_data(ImageChart.render(ImageChartSettings(key="img"), df))

    assert data["per_run_tags"] == {"1": {"0": {"run": "1"}}, "2": {"0": {"run": "2"}}}
    assert data["per_run_swatches"].keys() == {"1", "2"}
    assert data["x_axis"] == "step"


def test_render_shows_quantity_tags_in_the_units_and_precision_the_chart_asks_for() -> None:
    took = Quantity(value=5400, unit=TimeUnit.SECONDS)
    df = pd.DataFrame(
        [
            {
                "run_id": 1,
                "step": 0,
                artifact_column("img"): "ref://1-0",
                artifact_tags_column("img"): {"took": took},
            }
        ]
    )
    settings = ImageChartSettings(key="img", tag_time_unit=TimeUnit.MINUTES, tag_precision=2)

    assert _captions_store_data(ImageChart.render(settings, df)) == {"1": {"0": "took: 90 min"}}


def test_render_applies_the_per_tag_display_map_over_the_chart_wide_default() -> None:
    took = Quantity(value=5400, unit=TimeUnit.SECONDS)
    df = pd.DataFrame(
        [
            {
                "run_id": 1,
                "step": 0,
                artifact_column("img"): "ref://1-0",
                artifact_tags_column("img"): {"took": took, "wait": took},
            }
        ]
    )
    settings = ImageChartSettings(
        key="img", tag_time_unit=TimeUnit.MINUTES, tag_display={"took": TagDisplay(time_unit=TimeUnit.HOURS)}
    )

    assert _captions_store_data(ImageChart.render(settings, df)) == {
        "1": {"0": "took: 1.500 h, wait: 90.00 min"}
    }


def test_render_handles_missing_tags_for_some_rows_without_crashing() -> None:
    df = pd.DataFrame(
        [
            {
                "run_id": 1,
                "step": 0,
                artifact_column("img"): "ref://1-0",
                artifact_tags_column("img"): {"split": "train"},
            },
            {"run_id": 1, "step": 1, artifact_column("img"): "ref://1-1", artifact_tags_column("img"): None},
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
    return _props(_props(stack)["children"][2])  # pyrefly: ignore [unknown-argument-type]


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
    df = pd.DataFrame([{"run_id": 1, "step": s, artifact_column("img"): f"ref://{s}"} for s in range(200)])
    stack = ImageChart.render(ImageChartSettings(key="img"), df)

    slider = _slider_props(stack)
    labeled = [m for m in slider["marks"] if "label" in m]
    assert len(labeled) <= MAX_SLIDER_LABELS + 2


# ---- click-to-zoom modal + native-size thumbnails ----


def _first_run_block(stack: object) -> dict[str, Any]:
    # children: [Store, Slider, *page_grids, pager, Modal]; first page grid holds the run blocks.
    first_grid = _props(stack)["children"][3]  # pyrefly: ignore [unknown-variable-type]
    return _props(_props(first_grid)["children"][0])  # pyrefly: ignore [unknown-argument-type]


def test_render_wraps_each_thumbnail_in_a_clickable_div() -> None:
    df = _artifacts_df(n_runs=1)
    stack = ImageChart.render(ImageChartSettings(key="img"), df)

    run_block = _first_run_block(stack)
    thumb_wrapper = _props(run_block["children"][1])  # pyrefly: ignore [unknown-argument-type]
    assert thumb_wrapper["id"] == {
        "type": "image-series-thumb",
        "instance": thumb_wrapper["id"]["instance"],
        "run": "1",
    }
    assert thumb_wrapper["n_clicks"] == 0


def test_run_block_label_defaults_to_run_id_without_a_run_name_column() -> None:
    df = _artifacts_df(n_runs=1)
    stack = ImageChart.render(ImageChartSettings(key="img"), df)

    run_block = _first_run_block(stack)
    assert _props(run_block["children"][0])["children"] == "Run 1"  # pyrefly: ignore [unknown-argument-type]


def test_run_block_label_uses_the_run_name_column_when_present() -> None:
    """`fetch_panel_dataframe` merges a `run_name` column onto the dataframe for charts that want
    to label by name instead of bare id -- this one slices down to an explicit `select_cols`
    before any further processing, so it must be told to keep that column."""
    df = _artifacts_df(n_runs=1)
    df[RUN_NAME_COLUMN] = "uptight-yak"
    stack = ImageChart.render(ImageChartSettings(key="img"), df)

    run_block = _first_run_block(stack)
    assert (
        # pyrefly: ignore [unknown-argument-type]
        _props(run_block["children"][0])["children"] == "uptight-yak"
    )


def test_render_includes_a_zoom_modal() -> None:
    df = _artifacts_df(n_runs=1)
    stack = ImageChart.render(ImageChartSettings(key="img"), df)

    modal = _props(_props(stack)["children"][-1])  # pyrefly: ignore [unknown-argument-type]
    assert modal["id"]["type"] == "image-series-modal"
    assert modal["opened"] is False


def test_native_size_caps_thumbnail_height_without_upscaling() -> None:
    df = _artifacts_df(n_runs=1)
    stack = ImageChart.render(ImageChartSettings(key="img", height=150, native_size=True), df)

    run_block = _first_run_block(stack)
    thumb = _props(_props(run_block["children"][1])["children"][0])  # pyrefly: ignore [unknown-argument-type]
    assert thumb["h"] is None
    assert thumb["style"]["maxHeight"] == "150px"


def test_caption_is_not_dimmed() -> None:
    df = _artifacts_df(n_runs=1)
    stack = ImageChart.render(ImageChartSettings(key="img"), df)

    run_block = _first_run_block(stack)
    caption = _props(run_block["children"][2])  # pyrefly: ignore [unknown-argument-type]
    assert "c" not in caption


def test_natural_width_derives_from_height_and_grid_cols() -> None:
    settings = ImageChartSettings(key="img", height=200)

    assert ImageChart.natural_width(settings) == DEFAULT_RENDER_CONTEXT.columns * (200 + 40)
    assert ImageChart.natural_width_in(settings, RenderContext(columns=5)) == 5 * (200 + 40)


def test_natural_width_override() -> None:
    assert ImageChart.natural_width(ImageChartSettings(key="img", height=200, width=333)) == 333


def test_the_per_tag_display_setting_is_described_as_a_table_of_its_display_fields() -> None:
    field = ChartTypeRegistry.get_registered_chart_types()[ImageChart.name]["tag_display"]

    assert field.type == ParameterFieldType.TABLE
    assert [(c.name, c.type) for c in field.columns or ()] == [
        ("precision", ParameterFieldType.INT),
        ("time_unit", ParameterFieldType.STR),
        ("size_unit", ParameterFieldType.STR),
    ]
    assert field.default == {}


@pytest.mark.parametrize(
    ("n_runs", "panel_columns", "columns"),
    [(1, 3, 1), (2, 3, 2), (5, 3, 3), (5, 2, 2), (5, 4, 4)],
)
def test_render_titles_the_chart_with_its_key_and_follows_the_panels_columns_up_to_its_runs(
    n_runs: int, panel_columns: int, columns: int
) -> None:
    context = RenderContext(columns=panel_columns)
    stack = ImageChart.render_in(ImageChartSettings(key="img"), _artifacts_df(n_runs=n_runs), context)

    children = _props(stack)["children"]
    assert _props(children[0])["children"] == "img"  # pyrefly: ignore [unknown-argument-type]
    first_page = _props(children[3])  # pyrefly: ignore [unknown-argument-type]
    assert first_page["id"]["page"] == 0
    assert first_page["cols"] == columns


def test_an_overlays_chart_is_titled_with_its_image_and_name_not_its_key() -> None:
    key = overlay_key("lonely")  # an overlay with no image logged: charted as any other image
    df = _artifacts_df(n_runs=1).rename(
        columns={
            artifact_column("img"): artifact_column(key),
            artifact_tags_column("img"): artifact_tags_column(key),
        }
    )

    stack = ImageChart.render(ImageChartSettings(key=key), df)

    title = _props(_props(stack)["children"][0])["children"]  # pyrefly: ignore [unknown-argument-type]
    assert title == f"lonely · {UNNAMED_OVERLAY}"


def _store_data(stack: Any) -> dict[str, Any]:  # noqa: ANN401 -- a rendered Dash component
    store = next(c for c in _props(stack)["children"] if "per_run_steps" in (_props(c).get("data") or {}))
    return _props(store)["data"]


def _log_layer(df: pd.DataFrame, key: str, fname: str, *, kind: LayerKind | None = None) -> None:
    """Log `key` at every run's step 0 as an artifact called `fname` (tagged as a layer of `kind`, if given)."""
    here = [s == 0 for s in df["step"]]
    df[artifact_column(key)] = [
        f"{key}://{r}" if at else None for r, at in zip(df["run_id"], here, strict=True)
    ]
    df[artifact_fname_column(key)] = [fname if at else None for at in here]
    tags: dict[str, str] = {LAYER_KIND_TAG: kind.value} if kind else {}
    df[artifact_tags_column(key)] = pd.Series(
        [tags if at else None for at in here], index=df.index, dtype=object
    )


def test_every_overlay_logged_over_a_picture_is_drawn_below_the_other_layers_its_rows_name() -> None:
    df = _artifacts_df(n_runs=2, steps_per_run=2)
    truth, unnamed = overlay_key("img", "truth"), overlay_key("img")
    _log_layer(df, truth, "truth.png")
    _log_layer(df, unnamed, "boxes.json", kind=LayerKind.SHAPES)
    _log_layer(df, "mask", "mask.png")
    _log_layer(df, overlay_key("other"), "other.json", kind=LayerKind.SHAPES)  # an overlay of another picture
    settings = ImageChartSettings(
        key="img", layers={"mask": LayerStyle()}
    )  # an artifact is a layer once it has a row

    overlays = _store_data(ImageChart.render(settings, df))["overlays"]

    assert overlays == {
        f"ref://{run}-0": [
            {"url": f"{unnamed}://{run}", "kind": "shapes", "layer": unnamed},
            {"url": f"{truth}://{run}", "kind": "picture", "layer": truth},
            {"url": f"mask://{run}", "kind": "picture", "layer": "mask"},
        ]
        for run in (1, 2)
    }  # step 1 logged no layers, so its pictures are not listed


def test_the_chart_says_how_each_layer_is_by_default_for_the_browser_to_apply_a_viewers_changes_over() -> (
    None
):
    df = _artifacts_df(n_runs=1)
    _log_layer(df, overlay_key("img", "truth"), "truth.png")
    _log_layer(df, overlay_key("img", "pred"), "pred.png")
    _log_layer(df, overlay_key("img", "gone"), "gone.png")
    pred, truth, gone = overlay_key("img", "pred"), overlay_key("img", "truth"), overlay_key("img", "gone")
    settings = ImageChartSettings(
        key="img",
        layers={
            truth: LayerStyle(visibility=LayerVisibility.HIDDEN),
            pred: LayerStyle(opacity=0.5),
            gone: LayerStyle(visibility=LayerVisibility.OFF),
        },
    )

    root = _props(ImageChart.render_in(settings, df, RenderContext(viewer_state_key="abc")))

    assert json.loads(root["data-dl-defaults"]) == {  # no "gone": a removed layer is not listed at all
        pred: {"hidden": False, "opacity": 0.5, "label": "pred"},
        truth: {"hidden": True, "opacity": 1.0, "label": "truth"},
    }
    assert root["data-dl-view-key"] == "abc"
    # A hidden layer is still listed with its picture, for a viewer to show; a removed one is not there at all.
    assert [
        layer["layer"] for layer in _store_data(ImageChart.render(settings, df))["overlays"]["ref://1-0"]
    ] == [pred, truth]


def test_each_picture_has_a_legend_to_be_filled_in_only_when_the_chart_has_layers() -> None:
    df = _artifacts_df(n_runs=3)

    assert _legends(ImageChart.render(ImageChartSettings(key="img"), df)) == 0

    _log_layer(df, overlay_key("img", "truth"), "truth.png")

    # One over each run's picture, and one over the enlarged view's.
    assert _legends(ImageChart.render(ImageChartSettings(key="img"), df)) == 3 + 1


def _is_legend(props: dict[str, Any]) -> bool:
    return props.get("className") == "dl-layer-legend"


def _legends(node: Any) -> int:  # noqa: ANN401 -- a rendered Dash component tree
    """How many legends (`dl-layer-legend`) there are under `node`."""
    return len(find_all_props(node, _is_legend))


def test_what_a_viewer_showed_or_hid_is_made_the_charts_own_except_for_a_removed_layer() -> None:
    settings = ImageChartSettings(
        key="img",
        layers={
            "truth": LayerStyle(visibility=LayerVisibility.HIDDEN, opacity=0.5),
            "gone": LayerStyle(visibility=LayerVisibility.OFF),
        },
    )
    state = ImageViewerState(
        layers={
            "truth": ViewedLayer(hidden=False),  # shown now: its opacity is the chart's own, and stays
            "pred": ViewedLayer(hidden=True),  # a layer with no row yet gets one
            "gone": ViewedLayer(hidden=False),  # not theirs to bring back
        }
    )

    saved = state.applied_to(settings)

    assert saved.layers == {
        "truth": LayerStyle(opacity=0.5),
        "gone": LayerStyle(visibility=LayerVisibility.OFF),
        "pred": LayerStyle(visibility=LayerVisibility.HIDDEN),
    }


def test_the_registry_saves_a_viewers_state_under_the_charts_own_id() -> None:
    chart = ChartInstance(chart_type=ImageChart.name, parameters={"key": "img"})

    saved = ChartTypeRegistry.saved_with_viewer_state(chart, {"layers": {"truth": {"hidden": True}}})

    assert saved.id == chart.id
    assert ChartTypeRegistry.keeps_viewer_state(chart)
    assert ImageChartSettings.model_validate(saved.parameters).layers == {
        "truth": LayerStyle(visibility=LayerVisibility.HIDDEN)
    }


def test_a_chart_that_keeps_no_viewer_state_says_so_and_cannot_be_saved_with_any() -> None:
    chart = ChartInstance(chart_type="line", parameters={"column": "loss", "x_axis": "step"})

    assert not ChartTypeRegistry.keeps_viewer_state(chart)
    with pytest.raises(ValueError, match="keeps no viewer state"):
        ChartTypeRegistry.saved_with_viewer_state(chart, {})


def test_a_viewers_change_must_be_a_change_to_a_layer() -> None:
    chart = ChartInstance(chart_type=ImageChart.name, parameters={"key": "img"})

    with pytest.raises(ValidationError):
        ChartTypeRegistry.saved_with_viewer_state(chart, {"layers": {"truth": {"opacity": 3}}})


@pytest.mark.parametrize(
    "layer_key",
    [
        pytest.param("boxes", id="an-artifact-not-listed-as-a-layer"),
        pytest.param(f"img{OVERLAY_KEY_SUFFIX}x", id="a-key-that-only-starts-like-an-overlay"),
        pytest.param(overlay_key("img2"), id="an-overlay-of-another-picture"),
    ],
)
def test_an_image_chart_draws_nothing_over_its_pictures_that_is_not_its_own_overlay(layer_key: str) -> None:
    df = _artifacts_df(n_runs=1)
    _log_layer(df, layer_key, "layer.json", kind=LayerKind.SHAPES)

    assert _store_data(ImageChart.render(ImageChartSettings(key="img"), df))["overlays"] == {}


def test_a_removed_layer_is_not_drawn_listed_or_left_for_a_viewer_to_bring_back() -> None:
    df = _artifacts_df(n_runs=1)
    _log_layer(df, overlay_key("img", "truth"), "truth.png")
    _log_layer(df, "mask", "mask.png")
    settings = ImageChartSettings(
        key="img",
        layers={
            overlay_key("img", "truth"): LayerStyle(visibility=LayerVisibility.OFF),
            "mask": LayerStyle(visibility=LayerVisibility.OFF),
        },
    )

    rendered = ImageChart.render(settings, df)

    assert _store_data(rendered)["overlays"] == {}
    assert _legends(rendered) == 0


class _Choices:
    """What an experiment has, as a chart type is told it when its settings are to be edited."""

    def __init__(self, keys: list[str]) -> None:
        self._keys = keys

    def options(self, kind: ColumnKind) -> list[str]:
        return []

    def artifact_keys(self) -> list[str]:
        return self._keys


def test_every_overlay_of_the_picture_is_a_fixed_row_of_the_layers_table_by_its_name() -> None:
    truth, pred = overlay_key("img", "truth"), overlay_key("img", "pred")

    fixed = ImageChart.fixed_table_rows(
        ImageChartSettings(key="img"),
        _Choices([truth, overlay_key("img"), overlay_key("other", "x"), pred, "mask"]),
    )

    assert (
        fixed
        == {  # this picture's overlays, in key order, by their names; not another's, nor a plain artifact
            "layers": {overlay_key("img"): UNNAMED_OVERLAY, pred: "pred", truth: "truth"}
        }
    )


def test_what_the_editor_saves_for_a_fixed_row_is_what_the_chart_draws_without_one() -> None:
    """The editor lists a blank row for every overlay and saves what it lists: that must be no change to the chart."""
    df = _artifacts_df(n_runs=1)
    truth, pred = overlay_key("img", "truth"), overlay_key("img", "pred")
    _log_layer(df, truth, "truth.png")
    _log_layer(df, pred, "pred.png")
    settings = ImageChartSettings(key="img", layers={truth: LayerStyle(visibility=LayerVisibility.HIDDEN)})

    saved = ImageChartSettings.model_validate(
        {"key": "img", "layers": {pred: {}, truth: {"visibility": "hidden"}, "mask": {"opacity": 0.3}}}
    )

    assert saved == settings.model_copy(
        update={"layers": {**settings.layers, "mask": LayerStyle(opacity=0.3)}}
    )
    assert _store_data(ImageChart.render(saved, df)) == _store_data(ImageChart.render(settings, df))


def test_a_chart_that_will_not_validate_has_no_fixed_rows() -> None:
    chart = ChartInstance(chart_type=ImageChart.name, parameters={"key": "img", "height": "tall"})

    assert ChartTypeRegistry.fixed_table_rows(chart, _Choices([overlay_key("img", "truth")])) == {}


@pytest.mark.parametrize(
    ("saved", "layers"),
    [
        pytest.param({}, {}, id="none"),
        pytest.param({overlay_key("img"): {}}, {}, id="an-own-overlay-in-full-is-drawn-anyway"),
        pytest.param(
            {overlay_key("img", "truth"): {"opacity": 0.5}},
            {overlay_key("img", "truth"): LayerStyle(opacity=0.5)},
            id="an-own-overlay-keeps-its-opacity-under-its-key",
        ),
        pytest.param(
            {overlay_key("img"): {"opacity": 0.5}},
            {overlay_key("img"): LayerStyle(opacity=0.5)},
            id="the-unnamed-overlay",
        ),
        pytest.param(
            {"mask": {"opacity": 0.3}, "boxes": {}},
            {"mask": LayerStyle(opacity=0.3), "boxes": LayerStyle()},
            id="any-other-artifact-is-a-layer-of-its-own-in-order",
        ),
        pytest.param(
            {overlay_key("other"): {}},
            {overlay_key("other"): LayerStyle()},
            id="an-overlay-of-another-picture-is-a-layer-of-its-own",
        ),
    ],
)
def test_a_chart_saved_with_an_overlays_row_per_layer_draws_the_same_layers(
    saved: dict[str, dict[str, float]], layers: dict[str, LayerStyle]
) -> None:
    settings = ImageChartSettings.model_validate({"key": "img", "overlays": saved})

    assert settings == ImageChartSettings(key="img", layers=layers)


@pytest.mark.parametrize(
    ("layers", "saved"),
    [
        pytest.param({overlay_key("img", "a"): {}}, {}, id="an-overlay-as-it-is-drawn-anyway-is-not-saved"),
        pytest.param({overlay_key("img"): {}}, {}, id="nor-is-the-unnamed-one"),
        pytest.param(
            {overlay_key("img", "a"): {"opacity": 0.5}},
            {overlay_key("img", "a"): LayerStyle(opacity=0.5)},
            id="an-overlay-treated-differently-is",
        ),
        pytest.param(
            {overlay_key("other", "a"): {}},
            {overlay_key("other", "a"): LayerStyle()},
            id="another-pictures-overlay-is-a-layer-of-its-own",
        ),
        pytest.param({"mask": {}}, {"mask": LayerStyle()}, id="any-other-artifact-is"),
    ],
)
def test_only_the_layers_that_are_not_drawn_anyway_are_saved(
    layers: dict[str, dict[str, float]], saved: dict[str, LayerStyle]
) -> None:
    assert ImageChartSettings.model_validate({"key": "img", "layers": layers}).layers == saved


def test_a_layers_opacity_is_a_fraction() -> None:
    with pytest.raises(ValidationError):
        LayerStyle.model_validate({"opacity": 1.5})


def test_the_same_settings_under_different_chart_ids_get_different_component_ids() -> None:
    """The editor's preview and the saved chart have the same settings; sharing ids broke the saved chart's callbacks."""
    settings = ImageChartSettings(key="img")

    def store_instance(chart_id: str) -> str:
        stack = ImageChart.render_in(settings, _artifacts_df(n_runs=1), RenderContext(chart_id=chart_id))
        store = next(c for c in _props(stack)["children"] if "per_run_steps" in (_props(c).get("data") or {}))
        return _props(store)["id"]["instance"]

    assert store_instance("preview") != store_instance("saved")
    assert store_instance("saved") == store_instance("saved")


def test_hiding_every_overlay_is_not_part_of_what_a_viewer_can_save() -> None:
    with pytest.raises(ValidationError):
        ImageViewerState.model_validate({"overlays_hidden": True})
