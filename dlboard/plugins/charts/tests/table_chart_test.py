"""Tests for what the table chart renders: a row per run, per value of a metric, or per artifact."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from dlboard.conftest import props as _props
from dlboard.models import (
    HPARAM_COLUMN_PREFIX,
    LAYER_KIND_TAG,
    RUN_NAME_COLUMN,
    LayerKind,
    RenderContext,
    artifact_column,
    artifact_fname_column,
    artifact_tags_column,
    overlay_key,
)
from dlboard.plugins.charts._table_settings import Aggregate, TableChartSettings, TableRows
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.serve import series_swatch_class
from dlboard.serve._grid import DEFAULT_TABLE_FONT_SIZE

GAP = float("nan")
_EVERYTHING = {"show_metrics": ["*"], "show_hparams": ["*"]}
"""Every metric and hyperparameter shown: what a table showed by default before a new table started with none."""
LR = f"{HPARAM_COLUMN_PREFIX}lr"
BATCH_SIZE = f"{HPARAM_COLUMN_PREFIX}batch_size"


def _render(settings: TableChartSettings, dataframe: pd.DataFrame) -> dict[str, Any]:
    return _props(TableChart.render(settings, dataframe))


def _rows_by_run(component: object) -> dict[int, dict[str, Any]]:
    return {row["run_id"]: row for row in _props(component)["rowData"]}


def _leaf_columns(component: object) -> list[dict[str, Any]]:
    """The grid's columns, with any column groups flattened away."""
    return [leaf for column in _props(component)["columnDefs"] for leaf in column.get("children", [column])]


def _wide_df() -> pd.DataFrame:
    """Two runs, two steps each, with a metric and a broadcast hparam column."""
    return pd.DataFrame(
        {"run_id": [1, 1, 2, 2], "step": [0, 1, 0, 1], "loss": [0.5, 0.4, 0.9, 0.7], LR: [0.1, 0.1, 0.2, 0.2]}
    )


def _artifacts_df() -> pd.DataFrame:
    """Two runs' artifacts: an image per run (run 1 logging two) with tags, and a text file."""
    return pd.DataFrame(
        {
            "run_id": [1, 1, 2],
            "step": [0, 1, 0],
            "loss": [0.5, 0.4, 0.9],
            artifact_column("eval/a.png"): ["/artifact/1?v=a", "/artifact/2?v=b", "/artifact/3?v=c"],
            artifact_fname_column("eval/a.png"): ["a.png", "a.png", "a.png"],
            artifact_tags_column("eval/a.png"): [
                {"split": "val", "score": 0.5},
                {"split": "val", "score": 0.9},
                {"split": "train", "took": {"value": 2, "unit": "min"}},
            ],
            artifact_column("eval/notes.txt"): [GAP, GAP, "/artifact/4?v=d"],
            artifact_fname_column("eval/notes.txt"): [GAP, GAP, "notes.txt"],
            artifact_tags_column("eval/notes.txt"): [GAP, GAP, {}],
        }
    )


def _epochs_df() -> pd.DataFrame:
    """Two runs of four steps. `epoch` is logged on the first step of each epoch, an image on the last."""
    return pd.DataFrame(
        {
            "run_id": [1] * 4 + [2] * 4,
            "step": [0, 1, 2, 3] * 2,
            "epoch": [0, GAP, 1, GAP] * 2,
            "loss": [0.8, 0.6, 0.4, 0.2, 0.9, 0.7, 0.5, 0.3],
            artifact_column("img"): [GAP, "/a/1", GAP, "/a/2", GAP, "/a/3", GAP, "/a/4"],
            artifact_fname_column("img"): [GAP, "a.png", GAP, "b.png", GAP, "c.png", GAP, "d.png"],
        }
    )


def _image(url: str, name: str) -> str:
    return f"[![{name}]({url})]({url})"


# ---- one row per run ----


def test_render_empty_dataframe_shows_a_message() -> None:
    [row] = _render(TableChartSettings(**_EVERYTHING), pd.DataFrame())["rowData"]
    assert "No data" in row["message"]


def test_a_run_row_shows_each_metrics_own_latest_value() -> None:
    """A metric logged less often than the last step (a per-epoch one) is not blank for it."""
    df = pd.DataFrame(
        {"run_id": [1, 1, 1], "step": [0, 1, 2], "loss": [0.5, 0.4, 0.3], "val_acc": [0.6, GAP, GAP]}
    )
    [row] = _render(TableChartSettings(**_EVERYTHING), df)["rowData"]

    assert row["loss"] == pytest.approx(0.3)
    assert row["val_acc"] == pytest.approx(0.6)


def test_a_run_row_does_not_show_bookkeeping_columns() -> None:
    row = _rows_by_run(
        TableChart.render(TableChartSettings(**_EVERYTHING), _wide_df().assign(timestamp_utc=pd.Timestamp(0)))
    )[1]
    assert not {"step", "timestamp_utc"} & row.keys()


def test_a_metric_can_be_asked_for_by_name_even_when_it_is_bookkeeping() -> None:
    row = _rows_by_run(TableChart.render(TableChartSettings(show_metrics=["step"]), _wide_df()))[1]
    assert row["step"] == 1


def test_hparam_columns_are_titled_without_the_prefix_and_apart_from_a_metric_of_the_same_name() -> None:
    table = TableChart.render(
        TableChartSettings(**_EVERYTHING), _wide_df().assign(lr=[0.01, 0.02, 0.03, 0.04])
    )
    headers = {column["field"]: column["headerName"] for column in _leaf_columns(table)}
    row = _rows_by_run(table)[1]

    assert headers[LR] == "lr"
    assert (row[LR], row["lr"]) == (pytest.approx(0.1), pytest.approx(0.02))


def test_a_dotted_metric_name_is_never_read_as_a_nested_field() -> None:
    props = _render(TableChartSettings(**_EVERYTHING), _wide_df().rename(columns={"loss": "val.loss"}))
    assert props["dashGridOptions"]["suppressFieldDotNotation"] is True


@pytest.mark.parametrize(
    ("settings", "shown", "hidden"),
    [
        pytest.param(
            TableChartSettings(show_metrics=["acc"]), {"acc"}, {"loss", "lr_decay"}, id="metric-name"
        ),
        pytest.param(
            TableChartSettings(show_metrics=["l*"]), {"loss", "lr_decay"}, {"acc"}, id="metric-prefix"
        ),
        pytest.param(
            TableChartSettings(show_metrics=["acc", "l*"]), {"acc", "loss", "lr_decay"}, set(), id="both"
        ),
        pytest.param(
            TableChartSettings(show_metrics=[], show_hparams=["*"]),
            set(),
            {"loss", "acc", "lr_decay"},
            id="no-metrics",
        ),
        pytest.param(TableChartSettings(show_hparams=["lr"]), {LR}, {BATCH_SIZE}, id="hparam"),
        pytest.param(
            TableChartSettings(show_metrics=["*"], show_hparams=[]), set(), {LR, BATCH_SIZE}, id="no-hparams"
        ),
    ],
)
def test_a_run_row_shows_the_metrics_and_hparams_the_settings_pick(
    settings: TableChartSettings, shown: set[str], hidden: set[str]
) -> None:
    df = _wide_df().assign(acc=0.6, lr_decay=0.9, **{BATCH_SIZE: 8})
    row = _rows_by_run(TableChart.render(settings, df))[1]

    assert shown <= row.keys()
    assert not hidden & row.keys()


def test_picking_an_artifact_does_not_bring_every_hyperparameter_along() -> None:
    """Showing no hyperparameters is a choice a table can make, artifacts or not."""
    settings = TableChartSettings(show_metrics=[], show_hparams=[], show_artifacts=["eval/a.png"])
    table = TableChart.render(settings, _artifacts_df().assign(**{LR: 0.1}))

    assert [column["field"] for column in _leaf_columns(table)][:2] == [
        RUN_NAME_COLUMN,
        artifact_column("eval/a.png"),
    ]
    assert not any(column["field"] in {LR, "loss"} for column in _leaf_columns(table))


def test_a_run_row_leaves_out_artifacts_that_are_not_picked() -> None:
    row = _rows_by_run(TableChart.render(TableChartSettings(**_EVERYTHING), _artifacts_df()))[1]
    assert not any(field.startswith("artifact__") for field in row)


def test_a_run_row_names_the_run_and_colors_it_as_the_charts_do() -> None:
    df = _wide_df().assign(**{RUN_NAME_COLUMN: pd.Series(["one", "one", None, None], dtype=object)})
    table = TableChart.render(TableChartSettings(**_EVERYTHING), df)
    rows = _rows_by_run(table)
    run_column = _leaf_columns(table)[0]

    assert rows[1][RUN_NAME_COLUMN] == "one"
    assert rows[2][RUN_NAME_COLUMN] == "Run 2"  # unnamed: the id, as everywhere else
    assert rows[2]["_swatch"] == series_swatch_class(2)
    assert (run_column["field"], run_column["pinned"]) == (RUN_NAME_COLUMN, "left")
    assert [column["field"] for column in _leaf_columns(table)].count(RUN_NAME_COLUMN) == 1


def test_a_run_row_groups_hparams_and_metrics_under_their_headers() -> None:
    groups: list[dict[str, Any]] = _render(
        TableChartSettings(**_EVERYTHING, aggregate=Aggregate.MAX), _wide_df()
    )["columnDefs"][1:]
    assert [group["headerName"] for group in groups] == ["Hyperparameters", "Metrics (max)"]


@pytest.mark.parametrize(
    ("aggregate", "run_1_loss"),
    [
        pytest.param(Aggregate.LATEST, 0.4),
        pytest.param(Aggregate.FIRST, 0.5),
        pytest.param(Aggregate.MIN, 0.4),
        pytest.param(Aggregate.MAX, 0.5),
        pytest.param(Aggregate.MEAN, 0.45),
    ],
)
def test_a_run_row_reduces_a_metric_as_the_aggregate_says(aggregate: Aggregate, run_1_loss: float) -> None:
    table = TableChart.render(TableChartSettings(**_EVERYTHING, aggregate=aggregate), _wide_df())
    assert _rows_by_run(table)[1]["loss"] == pytest.approx(run_1_loss)


def test_a_metric_that_is_not_a_number_is_never_averaged() -> None:
    df = _wide_df().assign(timestamp_utc=["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"])
    settings = TableChartSettings(show_metrics=["timestamp_utc"], aggregate=Aggregate.MEAN)
    assert _rows_by_run(TableChart.render(settings, df))[1]["timestamp_utc"] == "2026-01-02"


def test_a_run_row_without_a_step_column_dedupes_by_run() -> None:
    df = pd.DataFrame({"run_id": [1, 1], LR: [0.1, 0.1]})
    assert len(_render(TableChartSettings(**_EVERYTHING), df)["rowData"]) == 1


def test_font_size_defaults_to_the_shared_smaller_one_and_can_be_set_in_pixels() -> None:
    assert (
        _render(TableChartSettings(**_EVERYTHING), _wide_df())["style"]["--ag-font-size"]
        == DEFAULT_TABLE_FONT_SIZE
    )
    assert _render(TableChartSettings(font_size=20), _wide_df())["style"]["--ag-font-size"] == "20px"


def test_natural_width_is_flat_unless_set() -> None:
    assert TableChart.natural_width(TableChartSettings(**_EVERYTHING)) == 700
    assert TableChart.natural_width(TableChartSettings(width=333)) == 333


# ---- one row per value of a metric ----


def test_rows_of_a_field_have_a_row_per_run_and_value() -> None:
    settings = TableChartSettings(show_metrics=["*"], rows=TableRows.FIELD, row_field="step", show_hparams=[])
    props = _render(settings, _wide_df())

    assert [row["_row_id"] for row in props["rowData"]] == ["1/0", "1/1", "2/0", "2/1"]
    assert [column["field"] for column in _leaf_columns(TableChart.render(settings, _wide_df()))] == [
        RUN_NAME_COLUMN,
        "step",
        "loss",
    ]


@pytest.mark.parametrize(
    ("spread_by_run", "max_rows", "drawn"),
    [
        pytest.param(False, 4, True, id="a-row-per-run-and-value-up-to-the-limit"),
        pytest.param(False, 3, False, id="past-it-says-how-many-instead"),
        pytest.param(True, 3, True, id="runs-side-by-side-are-a-row-per-value"),
    ],
)
def test_a_table_of_a_fields_values_past_the_row_limit_says_so_instead_of_sending_them(
    monkeypatch: pytest.MonkeyPatch, max_rows: int, *, spread_by_run: bool, drawn: bool
) -> None:
    """A row per step of a long run went to the browser whole: megabytes of rows for one table (#364)."""
    monkeypatch.setenv("DLBOARD_MAX_TABLE_ROWS", str(max_rows))
    settings = TableChartSettings(
        **_EVERYTHING, rows=TableRows.FIELD, row_field="step", spread_by_run=spread_by_run
    )

    rows = _render(settings, _wide_df())["rowData"]

    assert ("message" not in rows[0]) is drawn
    if not drawn:
        assert rows[0]["message"].startswith("A row for each step would be 4 rows, more than the 3")


def test_runs_spread_side_by_side_are_a_column_each_headed_by_the_runs_name_and_color() -> None:
    df = _wide_df().assign(**{RUN_NAME_COLUMN: ["one", "one", "two", "two"]})
    settings = TableChartSettings(
        rows=TableRows.FIELD, row_field="step", spread_by_run=True, show_metrics=["loss"]
    )
    props = _render(settings, df)
    columns = {column["field"]: column for column in props["columnDefs"]}
    row_by_step = {row["step"]: row for row in props["rowData"]}

    assert set(columns) == {"step", "1::loss", "2::loss"}
    assert columns["1::loss"]["headerName"] == "one"
    assert columns["2::loss"]["headerClass"] == series_swatch_class(2)
    assert row_by_step[0]["1::loss"] == pytest.approx(0.5)
    assert row_by_step[1]["2::loss"] == pytest.approx(0.7)


def test_runs_spread_side_by_side_group_several_values_under_each_run() -> None:
    settings = TableChartSettings(
        rows=TableRows.FIELD, row_field="step", spread_by_run=True, show_metrics=["loss", "acc"]
    )
    columns = _render(settings, _wide_df().assign(acc=0.5))["columnDefs"]

    [group_1, group_2] = columns[1:]
    assert [leaf["headerName"] for leaf in group_1["children"]] == ["loss", "acc"]
    assert group_2["headerName"] == "Run 2"


def test_runs_spread_side_by_side_leave_out_hparams() -> None:
    settings = TableChartSettings(rows=TableRows.FIELD, row_field="step", spread_by_run=True)
    assert LR not in str(_render(settings, _wide_df())["columnDefs"])


def test_a_run_with_nothing_at_a_value_leaves_its_cell_empty() -> None:
    df = _wide_df().iloc[[0, 1, 2]]
    settings = TableChartSettings(
        rows=TableRows.FIELD, row_field="step", spread_by_run=True, show_metrics=["loss"]
    )
    row_by_step = {row["step"]: row for row in _render(settings, df)["rowData"]}

    assert row_by_step[1].get("2::loss") is None


def test_rows_of_a_field_reduce_what_a_run_logged_for_each_value_as_the_aggregate_says() -> None:
    settings = TableChartSettings(
        rows=TableRows.FIELD,
        row_field="epoch",
        show_metrics=["loss"],
        show_hparams=[],
        aggregate=Aggregate.MEAN,
    )
    rows = {(row["run_id"], row["epoch"]): row for row in _render(settings, _epochs_df())["rowData"]}

    assert rows[(1, 0)]["loss"] == pytest.approx(0.7)  # steps 0 and 1
    assert rows[(2, 1)]["loss"] == pytest.approx(0.4)  # steps 2 and 3


def test_each_epoch_shows_the_latest_artifact_of_that_epoch_from_each_run() -> None:
    """A row an epoch, a column a run -- though the image was logged on a step `epoch` was not."""
    settings = TableChartSettings(
        rows=TableRows.FIELD, row_field="epoch", spread_by_run=True, show_metrics=[], show_artifacts=["img"]
    )
    props = _render(settings, _epochs_df())
    row_by_epoch = {row["epoch"]: row for row in props["rowData"]}

    assert row_by_epoch[0]["1::artifact__img"] == _image("/a/1", "a.png")
    assert row_by_epoch[1]["1::artifact__img"] == _image("/a/2", "b.png")
    assert row_by_epoch[1]["2::artifact__img"] == _image("/a/4", "d.png")
    assert [column["headerName"] for column in props["columnDefs"]] == ["epoch", "Run 1", "Run 2"]


def test_a_value_of_the_row_field_that_nothing_shown_was_logged_under_is_no_row() -> None:
    df = _epochs_df().assign(step=[0, 1, 2, 3, 0, 1, 2, 3])
    df.loc[len(df)] = {"run_id": 1, "step": 9, "epoch": 7, "loss": GAP}
    settings = TableChartSettings(
        rows=TableRows.FIELD, row_field="epoch", show_metrics=["loss"], show_hparams=[]
    )

    assert 7 not in {row["epoch"] for row in _render(settings, df)["rowData"]}


def test_rows_of_a_field_nobody_logged_say_so() -> None:
    settings = TableChartSettings(rows=TableRows.FIELD, row_field="nope")
    [row] = _render(settings, _wide_df())["rowData"]
    assert "'nope'" in row["message"]


# ---- one row per artifact ----

_BY_KEY = TableChartSettings(
    rows=TableRows.ARTIFACT, show_artifacts=["eval/a.png"], show_metrics=[], show_hparams=[]
)
_BY_PREFIX = _BY_KEY.model_copy(update={"show_artifacts": ["eval/*"]})


@pytest.mark.parametrize(
    ("settings", "row_ids", "has_key_column"),
    [
        pytest.param(_BY_KEY, ["1/0/eval/a.png", "1/1/eval/a.png", "2/0/eval/a.png"], False, id="key"),
        pytest.param(
            _BY_PREFIX,
            ["1/0/eval/a.png", "1/1/eval/a.png", "2/0/eval/a.png", "2/0/eval/notes.txt"],
            True,
            id="prefix",
        ),
    ],
)
def test_rows_of_artifacts_have_a_row_per_artifact_under_the_key_or_prefix(
    settings: TableChartSettings, row_ids: list[str], *, has_key_column: bool
) -> None:
    table = TableChart.render(settings, _artifacts_df())

    assert [row["_row_id"] for row in _props(table)["rowData"]] == row_ids
    assert ("key" in {column["field"] for column in _leaf_columns(table)}) is has_key_column


def test_rows_of_artifacts_link_a_file_and_show_an_image_as_a_thumbnail() -> None:
    rows = {row["_row_id"]: row for row in _render(_BY_PREFIX, _artifacts_df())["rowData"]}

    assert rows["1/0/eval/a.png"]["artifact"] == _image("/artifact/1?v=a", "a.png")
    assert rows["2/0/eval/notes.txt"]["artifact"] == "[notes.txt](/artifact/4?v=d)"


def test_a_file_name_cannot_make_an_artifact_cell_write_markdown() -> None:
    df = _artifacts_df().assign(**{artifact_fname_column("eval/a.png"): "x](https://evil.example).png"})
    row: dict[str, Any] = _render(_BY_KEY, df)["rowData"][0]

    assert row["artifact"].startswith(r"[![x\]\(https://evil.example\).png](/artifact/1?v=a)")


def test_rows_of_artifacts_make_each_tag_a_column_that_sorts_as_what_it_is() -> None:
    table = TableChart.render(_BY_KEY, _artifacts_df())
    columns = {column["headerName"]: column for column in _leaf_columns(table)}
    rows = _props(table)["rowData"]

    assert columns["score"]["filter"] == "agNumberColumnFilter"
    assert columns["split"]["filter"] is True
    assert [row["artifact__tag__score"] for row in rows[:2]] == [0.5, 0.9]  # not "0.5000"
    # A duration is in its base unit whatever it was logged in, so two runs' are comparable.
    assert rows[2]["artifact__tag__took"] == pytest.approx(120)
    assert "took (s)" in columns


def test_rows_of_artifacts_show_what_their_run_logged_at_the_same_step() -> None:
    settings = _BY_KEY.model_copy(update={"show_metrics": ["loss"], "show_hparams": ["lr"]})
    table = TableChart.render(settings, _artifacts_df().assign(**{LR: [0.1, 0.1, 0.2]}))
    rows = _props(table)["rowData"]

    assert [(row["loss"], row[LR]) for row in rows] == [(0.5, 0.1), (0.4, 0.1), (0.9, 0.2)]


def test_rows_of_artifacts_with_nothing_logged_say_so() -> None:
    table = TableChart.render(_BY_KEY, _artifacts_df().drop(columns=[artifact_column("eval/a.png")]))
    assert "No artifacts" in _props(table)["rowData"][0]["message"]


def test_a_table_makes_room_for_thumbnails_only_when_it_has_some() -> None:
    notes = _BY_KEY.model_copy(update={"show_artifacts": ["eval/notes.txt"]})

    assert "rowHeight" in _render(_BY_KEY, _artifacts_df())["dashGridOptions"]
    assert "rowHeight" not in _render(notes, _artifacts_df())["dashGridOptions"]


# ---- artifacts beside a run ----


def test_a_run_row_adds_a_column_group_for_each_artifact_with_the_runs_latest() -> None:
    settings = TableChartSettings(show_metrics=["loss"], show_hparams=[], show_artifacts=["eval/a.png"])
    table = TableChart.render(settings, _artifacts_df())
    group = next(g for g in _props(table)["columnDefs"] if g.get("headerName") == "eval/a.png")

    assert _rows_by_run(table)[1][artifact_column("eval/a.png")] == _image("/artifact/2?v=b", "a.png")
    assert _rows_by_run(table)[1][f"{artifact_column('eval/a.png')}__tag__score"] == pytest.approx(0.9)
    assert "score" in [column["headerName"] for column in group["children"]]


def test_a_prefix_of_artifacts_is_a_column_group_for_each_key_under_it() -> None:
    settings = TableChartSettings(show_metrics=[], show_hparams=[], show_artifacts=["eval/*"])
    groups: list[dict[str, Any]] = _render(settings, _artifacts_df())["columnDefs"][1:]

    assert [group["headerName"] for group in groups] == ["eval/a.png", "eval/notes.txt"]


def test_an_overlays_column_group_is_headed_by_its_image_and_name_not_its_key() -> None:
    key = overlay_key("eval/a.png", "truth")
    renamed = {
        column("eval/a.png"): column(key)
        for column in (artifact_column, artifact_fname_column, artifact_tags_column)
    }
    settings = TableChartSettings(show_metrics=[], show_hparams=[], show_artifacts=[key])

    groups: list[dict[str, Any]] = _render(settings, _artifacts_df().rename(columns=renamed))["columnDefs"][
        1:
    ]

    assert [group["headerName"] for group in groups] == ["eval/a.png · truth"]


# ---- the state a redraw keeps ----


def test_the_grid_is_named_by_its_chart_so_it_keeps_its_state_across_a_redraw() -> None:
    props = _props(
        TableChart.render_in(TableChartSettings(**_EVERYTHING), _wide_df(), RenderContext(chart_id="abc"))
    )

    assert props["id"] == {"type": "dl-table-chart", "chart": "abc"}
    assert props["persisted_props"] == ["columnState", "filterModel"]
    assert {row["_row_id"] for row in props["rowData"]} == {"1", "2"}


@pytest.mark.parametrize(
    ("other", "same_state"),
    [
        pytest.param(TableChartSettings(**_EVERYTHING, page_size=50), True, id="same-columns"),
        pytest.param(TableChartSettings(show_metrics=["acc"]), False, id="other-columns"),
    ],
)
def test_a_grid_of_other_columns_starts_without_the_old_ones_state(
    other: TableChartSettings, *, same_state: bool
) -> None:
    df = _wide_df().assign(acc=[0.6, 0.7, 0.8, 0.9])
    before = _render(TableChartSettings(**_EVERYTHING), df)["persistence"]

    assert (before == _render(other, df)["persistence"]) is same_state


# ---- images with the layers logged over them ----

_PICTURE = "eval/a.png"
_BOXES = overlay_key(_PICTURE, "boxes")


def _picture_with_boxes_df() -> pd.DataFrame:
    """Run 1's picture at steps 0 and 1, with an overlay of shapes logged beside the first one only."""
    return pd.DataFrame(
        {
            "run_id": [1, 1],
            "step": [0, 1],
            artifact_column(_PICTURE): ["/artifact/1?v=a", "/artifact/2?v=b"],
            artifact_fname_column(_PICTURE): ["a.png", "a.png"],
            artifact_tags_column(_PICTURE): [{}, {}],
            artifact_column(_BOXES): ["/artifact/3?v=c", GAP],
            artifact_fname_column(_BOXES): ["boxes.json", GAP],
            artifact_tags_column(_BOXES): [{LAYER_KIND_TAG: LayerKind.SHAPES.value}, GAP],
        }
    )


def _link_of(row: dict[str, Any], field: str) -> dict[str, Any]:
    return row[f"{field}__link"]


@pytest.mark.parametrize(
    ("settings", "row_id", "field", "layers"),
    [
        pytest.param(
            TableChartSettings(rows=TableRows.ARTIFACT, show_artifacts=[_PICTURE], show_metrics=[]),
            f"1/0/{_PICTURE}",
            "artifact",
            [{"url": "/artifact/3?v=c", "kind": "shapes", "layer": _BOXES}],
            id="a-row-per-artifact-draws-the-overlay-logged-at-its-step",
        ),
        pytest.param(
            TableChartSettings(rows=TableRows.ARTIFACT, show_artifacts=[_PICTURE], show_metrics=[]),
            f"1/1/{_PICTURE}",
            "artifact",
            [],
            id="a-picture-the-overlay-was-not-logged-beside-has-none",
        ),
        pytest.param(
            TableChartSettings(rows=TableRows.RUN, show_artifacts=["eval/*"], show_metrics=[]),
            "1",
            artifact_column(_PICTURE),
            [],
            id="a-row-per-run-shows-its-latest-picture-and-what-was-logged-beside-it",
        ),
    ],
)
def test_a_picture_in_a_table_carries_the_layers_logged_beside_it_at_its_step(
    settings: TableChartSettings, row_id: str, field: str, layers: list[dict[str, str]]
) -> None:
    rows = {row["_row_id"]: row for row in _render(settings, _picture_with_boxes_df())["rowData"]}

    link = _link_of(rows[row_id], field)
    assert (link["image"], link["layers"]) == (True, layers)


def test_an_overlay_of_a_picture_that_is_shown_is_drawn_over_it_and_not_a_column_of_its_own() -> None:
    settings = TableChartSettings(rows=TableRows.ARTIFACT, show_artifacts=["eval/*"], show_metrics=[])

    rows = _render(settings, _picture_with_boxes_df())["rowData"]

    assert {row["key"] for row in rows} == {_PICTURE}  # not the overlay's JSON, as a row of its own


def test_an_overlay_chosen_without_its_picture_is_a_file_to_open() -> None:
    settings = TableChartSettings(rows=TableRows.ARTIFACT, show_artifacts=[_BOXES], show_metrics=[])

    [row] = _render(settings, _picture_with_boxes_df())["rowData"]

    assert _link_of(row, "artifact")["image"] is False


def test_a_picture_shown_by_its_key_has_its_overlays_fetched_with_it() -> None:
    settings = TableChartSettings(show_artifacts=[_PICTURE])

    assert TableChart.data_request(settings).artifact_prefixes == {overlay_key(_PICTURE)}
