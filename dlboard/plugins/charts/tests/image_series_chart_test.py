"""Tests for the paginated image-series chart's render/hint logic."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from dlboard.conftest import props as _props
from dlboard.models import (
    DEFAULT_RENDER_CONTEXT,
    RUN_NAME_COLUMN,
    ChartTypeRegistry,
    ParameterFieldType,
    Quantity,
    RenderContext,
    TagDisplay,
    TimeUnit,
    artifact_column,
    artifact_fname_column,
    artifact_tags_column,
)
from dlboard.plugins.charts.image_series import (
    MAX_SLIDER_LABELS,
    ImageChart,
    ImageChartSettings,
    OverlayLayer,
    _slider_marks,
)


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
    ("x_axis", "expected"),
    [("step", set[str]()), ("epoch", {"epoch"})],
)
def test_hint_required_columns(x_axis: str, expected: set[str]) -> None:
    assert ImageChart.hint_required_columns(ImageChartSettings(key="img", x_axis=x_axis)) == expected


def test_hint_required_artifact_keys() -> None:
    assert ImageChart.hint_required_artifact_keys(ImageChartSettings(key="img")) == {"img"}


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


def _store_data(stack: Any) -> dict[str, Any]:  # noqa: ANN401 -- a rendered Dash component
    store = next(c for c in _props(stack)["children"] if "per_run_steps" in (_props(c).get("data") or {}))
    return _props(store)["data"]


def _log_layer(df: pd.DataFrame, key: str, fname: str, *, at_step: int = 0) -> None:
    """Log `key` at every run's `at_step` as an artifact called `fname`, and at no other step."""
    here = [s == at_step for s in df["step"]]
    df[artifact_column(key)] = [
        f"{key}://{r}" if at else None for r, at in zip(df["run_id"], here, strict=True)
    ]
    df[artifact_fname_column(key)] = [fname if at else None for at in here]


def test_layers_are_paired_with_the_picture_logged_at_the_same_run_and_step_in_the_order_listed() -> None:
    df = _artifacts_df(n_runs=2, steps_per_run=2)
    _log_layer(df, "mask", "mask.png")
    _log_layer(df, "outline", "outline.jpg")
    settings = ImageChartSettings(
        key="img", overlays={"outline": OverlayLayer(), "mask": OverlayLayer(opacity=0.5)}
    )

    overlays = _store_data(ImageChart.render(settings, df))["overlays"]

    assert overlays == {
        f"ref://{run}-0": [
            {"url": f"outline://{run}", "opacity": 1.0},
            {"url": f"mask://{run}", "opacity": 0.5},
        ]
        for run in (1, 2)
    }  # step 1 logged no layers, so its pictures are not listed


def test_an_image_chart_without_layers_draws_nothing_over_its_pictures() -> None:
    df = _artifacts_df(n_runs=1)
    _log_layer(df, "mask", "mask.png")

    assert _store_data(ImageChart.render(ImageChartSettings(key="img"), df))["overlays"] == {}


@pytest.mark.parametrize("fname", ["model.ckpt", "notes.txt", "boxes.json"])
def test_a_file_that_is_no_image_is_not_drawn_as_a_layer(fname: str) -> None:
    df = _artifacts_df(n_runs=1)
    _log_layer(df, "file", fname)

    stack = ImageChart.render(ImageChartSettings(key="img", overlays={"file": OverlayLayer()}), df)

    assert _store_data(stack)["overlays"] == {}


def test_the_layers_are_charted_with_the_image_and_edited_as_a_table() -> None:
    settings = ImageChartSettings(
        key="img", overlays={"boxes": OverlayLayer(), "mask": OverlayLayer(opacity=0.3)}
    )

    assert ImageChart.hint_required_artifact_keys(settings) == {"img", "boxes", "mask"}
    field = ChartTypeRegistry.get_registered_chart_types()[ImageChart.name]["overlays"]
    assert field.type is ParameterFieldType.TABLE
    assert [column.name for column in field.columns or ()] == ["opacity"]


def test_a_layers_opacity_is_a_fraction() -> None:
    with pytest.raises(ValidationError):
        OverlayLayer.model_validate({"opacity": 1.5})


def test_the_same_settings_under_different_chart_ids_get_different_component_ids() -> None:
    """The editor's preview and the saved chart have the same settings; sharing ids broke the saved chart's callbacks."""
    settings = ImageChartSettings(key="img")

    def store_instance(chart_id: str) -> str:
        stack = ImageChart.render_in(settings, _artifacts_df(n_runs=1), RenderContext(chart_id=chart_id))
        store = next(c for c in _props(stack)["children"] if "per_run_steps" in (_props(c).get("data") or {}))
        return _props(store)["id"]["instance"]

    assert store_instance("preview") != store_instance("saved")
    assert store_instance("saved") == store_instance("saved")
