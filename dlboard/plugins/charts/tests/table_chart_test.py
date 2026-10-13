"""Tests for the table chart type: run-mode rows, pivot mode, and its column hints."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from dlboard.conftest import props as _props
from dlboard.models import RUN_NAME_COLUMN, RenderContext
from dlboard.plugins.charts._table_style import (
    DEFAULT_TABLE_FONT_SIZE,
    HPARAM_COLUMN_PREFIX,
    artifact_column,
    artifact_tags_column,
)
from dlboard.plugins.charts.table_chart import TableChart, TableChartSettings
from dlboard.serve import series_swatch_class


def _rows_by_run(component: object) -> dict[int, dict[str, Any]]:
    return {row["run_id"]: row for row in _props(component)["rowData"]}


def _leaf_columns(component: object) -> list[dict[str, Any]]:
    """The grid's columns, with any column groups flattened away."""
    return [leaf for column in _props(component)["columnDefs"] for leaf in column.get("children", [column])]


def _wide_df() -> pd.DataFrame:
    """Two runs, two steps each, with a metric and a broadcast hparam column."""
    return pd.DataFrame(
        {
            "run_id": [1, 1, 2, 2],
            "step": [0, 1, 0, 1],
            "loss": [0.5, 0.4, 0.9, 0.7],
            f"{HPARAM_COLUMN_PREFIX}lr": [0.1, 0.1, 0.2, 0.2],
        }
    )


# ---- hint_required_columns / hint_required_hparams / hint_required_artifact_keys ----


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        pytest.param(TableChartSettings(), None, id="unbounded-by-default"),
        pytest.param(
            TableChartSettings(metrics=["loss", "acc"]), {"loss", "acc", "step"}, id="metrics-filter"
        ),
        pytest.param(
            TableChartSettings(pivot_on="step", pivot_metric="loss"),
            {"step", "loss"},
            id="pivot-wants-its-columns",
        ),
        # Metrics and hparams are fetched independently -- curating one but leaving the other at its
        # default shouldn't force a full "every metric in the experiment" fetch for a table that isn't
        # even set up to show any.
        pytest.param(TableChartSettings(hparams=["lr"]), set(), id="only-hparams-curated"),
    ],
)
def test_hint_required_columns_follows_the_settings(
    settings: TableChartSettings, expected: set[str] | None
) -> None:
    assert TableChart.hint_required_columns(settings) == expected


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        pytest.param(TableChartSettings(), None, id="unbounded-by-default"),
        pytest.param(
            TableChartSettings(hparams=["lr", "batch_size"]), {"lr", "batch_size"}, id="hparams-filter"
        ),
        pytest.param(
            TableChartSettings(pivot_on="step", pivot_metric="loss"), set(), id="empty-in-pivot-mode"
        ),
    ],
)
def test_hint_required_hparams_follows_the_settings(
    settings: TableChartSettings, expected: set[str] | None
) -> None:
    assert TableChart.hint_required_hparams(settings) == expected


def test_hint_required_artifact_keys_always_empty() -> None:
    assert TableChart.hint_required_artifact_keys(TableChartSettings()) == set()


# ---- render: run mode ----


def test_render_empty_dataframe_shows_a_message() -> None:
    table = TableChart.render(TableChartSettings(), pd.DataFrame())
    rows = _props(table)["rowData"]
    assert len(rows) == 1
    assert "No data" in rows[0]["message"]


def test_render_run_mode_one_row_per_run_at_last_step() -> None:
    table = TableChart.render(TableChartSettings(), _wide_df())
    rows = _rows_by_run(table)

    assert set(rows) == {1, 2}
    assert rows[1]["loss"] == pytest.approx(0.4)  # last step (1) for run 1
    assert rows[2]["loss"] == pytest.approx(0.7)  # last step (1) for run 2


def test_render_run_mode_shows_each_metrics_own_latest_value() -> None:
    """A metric logged less often than the last step (a per-epoch one) is not blank for it."""
    df = pd.DataFrame(
        {"run_id": [1, 1, 1], "step": [0, 1, 2], "loss": [0.5, 0.4, 0.3], "val_acc": [0.6, None, None]}
    )
    rows = _rows_by_run(TableChart.render(TableChartSettings(), df))

    assert rows[1]["loss"] == pytest.approx(0.3)
    assert rows[1]["val_acc"] == pytest.approx(0.6)


def test_render_run_mode_does_not_show_bookkeeping_columns() -> None:
    table = TableChart.render(TableChartSettings(), _wide_df().assign(timestamp_utc=pd.Timestamp(0)))
    assert not {"step", "timestamp_utc"} & _rows_by_run(table)[1].keys()


def test_render_run_mode_titles_hparam_columns_without_the_prefix() -> None:
    table = TableChart.render(TableChartSettings(), _wide_df())
    headers = {column["field"]: column["headerName"] for column in _leaf_columns(table)}

    assert headers[f"{HPARAM_COLUMN_PREFIX}lr"] == "lr"
    assert _rows_by_run(table)[1][f"{HPARAM_COLUMN_PREFIX}lr"] == pytest.approx(0.1)


def test_render_run_mode_keeps_an_hparam_and_a_metric_of_the_same_name_apart() -> None:
    df = _wide_df().assign(lr=[0.01, 0.02, 0.03, 0.04])
    rows = _rows_by_run(TableChart.render(TableChartSettings(), df))

    assert rows[1]["lr"] == pytest.approx(0.02)
    assert rows[1][f"{HPARAM_COLUMN_PREFIX}lr"] == pytest.approx(0.1)


def test_render_never_reads_a_dotted_metric_name_as_a_nested_field() -> None:
    props = _props(TableChart.render(TableChartSettings(), _wide_df().rename(columns={"loss": "val.loss"})))
    assert props["dashGridOptions"]["suppressFieldDotNotation"] is True


def test_render_run_mode_metrics_filter_narrows_columns() -> None:
    df = _wide_df().assign(acc=[0.6, 0.7, 0.8, 0.9])
    table = TableChart.render(TableChartSettings(metrics=["acc"]), df)
    rows = _rows_by_run(table)

    assert "loss" not in rows[1]
    assert rows[1]["acc"] == pytest.approx(0.7)


def test_render_run_mode_hparams_filter_narrows_columns() -> None:
    df = _wide_df()
    df[f"{HPARAM_COLUMN_PREFIX}batch_size"] = [32, 32, 64, 64]
    table = TableChart.render(TableChartSettings(hparams=["lr"]), df)
    rows = _rows_by_run(table)

    assert f"{HPARAM_COLUMN_PREFIX}lr" in rows[1]
    assert f"{HPARAM_COLUMN_PREFIX}batch_size" not in rows[1]


def test_render_run_mode_with_only_hparams_curated_shows_no_metrics() -> None:
    """The panel's dataframe carries its sibling charts' metrics too; "hparams only" must not show them."""
    rows = _rows_by_run(TableChart.render(TableChartSettings(hparams=["lr"]), _wide_df()))
    assert "loss" not in rows[1]


def test_render_run_mode_excludes_artifact_columns() -> None:
    """An artifact's ref (a storage URL) and tags (a dict) aren't metric values -- neither is a table column."""
    df = _wide_df()
    df[artifact_column("img")] = ["ref://1-0", "ref://1-1", "ref://2-0", "ref://2-1"]
    df[artifact_tags_column("img")] = pd.Series(
        [{"split": "train"}, {"split": "train"}, None, None], dtype=object
    )
    table = TableChart.render(TableChartSettings(), df)
    rows = _rows_by_run(table)

    assert not {artifact_column("img"), artifact_tags_column("img")} & rows[1].keys()


def test_render_run_mode_names_each_run_and_colors_it_as_the_charts_do() -> None:
    df = _wide_df()
    df[RUN_NAME_COLUMN] = ["one", "one", None, None]
    table = TableChart.render(TableChartSettings(), df)
    rows = _rows_by_run(table)

    assert rows[1][RUN_NAME_COLUMN] == "one"
    assert rows[2][RUN_NAME_COLUMN] == "Run 2"  # unnamed: the id, as everywhere else
    assert rows[2]["_swatch"] == series_swatch_class(2)
    run_column = _leaf_columns(table)[0]
    assert run_column["field"] == RUN_NAME_COLUMN
    assert run_column["pinned"] == "left"
    assert [column["field"] for column in _leaf_columns(table)].count(RUN_NAME_COLUMN) == 1


def test_render_run_mode_groups_hparams_and_latest_metrics() -> None:
    groups: list[dict[str, Any]] = _props(TableChart.render(TableChartSettings(), _wide_df()))["columnDefs"][
        1:
    ]
    assert [group["headerName"] for group in groups] == ["Hyperparameters", "Metrics (latest)"]


def test_render_defaults_to_the_shared_smaller_font_size() -> None:
    table = TableChart.render(TableChartSettings(), _wide_df())
    assert _props(table)["style"]["--ag-font-size"] == DEFAULT_TABLE_FONT_SIZE


def test_render_font_size_override_applies_in_pixels() -> None:
    table = TableChart.render(TableChartSettings(font_size=20), _wide_df())
    assert _props(table)["style"]["--ag-font-size"] == "20px"


def test_render_run_mode_without_step_column_dedupes_by_run() -> None:
    df = pd.DataFrame({"run_id": [1, 1], f"{HPARAM_COLUMN_PREFIX}lr": [0.1, 0.1]})
    table = TableChart.render(TableChartSettings(), df)
    rows = _props(table)["rowData"]
    assert len(rows) == 1


# ---- render: pivot mode ----


def test_render_pivot_mode_one_row_per_pivot_value_one_column_per_run() -> None:
    table = TableChart.render(TableChartSettings(pivot_on="step", pivot_metric="loss"), _wide_df())
    props = _props(table)

    assert {c["field"] for c in props["columnDefs"]} == {"step", "1", "2"}
    row_by_step = {row["step"]: row for row in props["rowData"]}
    assert row_by_step[0]["1"] == pytest.approx(0.5)
    assert row_by_step[1]["2"] == pytest.approx(0.7)


def test_render_pivot_mode_without_pivot_metric_shows_a_message() -> None:
    table = TableChart.render(TableChartSettings(pivot_on="step"), _wide_df())
    rows = _props(table)["rowData"]
    assert len(rows) == 1
    assert "pivot_metric" in rows[0]["message"]


def test_render_pivot_mode_missing_columns_shows_a_message() -> None:
    table = TableChart.render(TableChartSettings(pivot_on="epoch", pivot_metric="loss"), _wide_df())
    rows = _props(table)["rowData"]
    assert len(rows) == 1
    assert "message" in rows[0]


# ---- natural_width ----


def test_natural_width_defaults_to_a_flat_value() -> None:
    assert TableChart.natural_width(TableChartSettings()) == 700


def test_natural_width_override() -> None:
    assert TableChart.natural_width(TableChartSettings(width=333)) == 333


# ---- run identity, and the state a redraw keeps ----


def test_pivot_mode_headers_each_run_by_name_and_color() -> None:
    df = _wide_df()
    df[RUN_NAME_COLUMN] = ["one", "one", "two", "two"]
    table = TableChart.render(TableChartSettings(pivot_on="step", pivot_metric="loss"), df)
    columns = {column["field"]: column for column in _props(table)["columnDefs"]}

    assert columns["1"]["headerName"] == "one"
    assert columns["2"]["headerClass"] == series_swatch_class(2)


def test_the_grid_is_named_by_its_chart_so_it_keeps_its_state_across_a_redraw() -> None:
    table = TableChart.render_in(TableChartSettings(), _wide_df(), RenderContext(chart_id="abc"))
    props = _props(table)

    assert props["id"] == {"type": "dl-table-chart", "chart": "abc"}
    assert props["persisted_props"] == ["columnState", "filterModel"]
    assert {row["_row_id"] for row in props["rowData"]} == {"1", "2"}


@pytest.mark.parametrize(
    ("other", "same_state"),
    [
        pytest.param(TableChartSettings(page_size=50), True, id="same-columns"),
        pytest.param(TableChartSettings(metrics=["acc"]), False, id="other-columns"),
    ],
)
def test_a_grid_of_other_columns_starts_without_the_old_ones_state(
    other: TableChartSettings, *, same_state: bool
) -> None:
    df = _wide_df().assign(acc=[0.6, 0.7, 0.8, 0.9])
    before = _props(TableChart.render(TableChartSettings(), df))["persistence"]
    after = _props(TableChart.render(other, df))["persistence"]

    assert (before == after) is same_state
