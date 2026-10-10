"""Tests for the table chart type: run-mode rows, pivot mode, and its column hints."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from dlboard.conftest import props as _props
from dlboard.models import RUN_NAME_COLUMN
from dlboard.plugins.charts._table_style import (
    DEFAULT_TABLE_FONT_SIZE,
    HPARAM_COLUMN_PREFIX,
    artifact_column,
    artifact_tags_column,
)
from dlboard.plugins.charts.table_chart import TableChart, TableChartSettings


def _rows_by_run(component: object) -> dict[int, dict[str, Any]]:
    return {row["run_id"]: row for row in _props(component)["rowData"]}


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


def test_hint_required_columns_defaults_to_unbounded() -> None:
    assert TableChart.hint_required_columns(TableChartSettings()) is None


def test_hint_required_columns_respects_metrics_filter() -> None:
    settings = TableChartSettings(metrics=["loss", "acc"])
    assert TableChart.hint_required_columns(settings) == {"loss", "acc", "step"}


def test_hint_required_columns_pivot_mode_wants_pivot_columns_only() -> None:
    settings = TableChartSettings(pivot_on="step", pivot_metric="loss")
    assert TableChart.hint_required_columns(settings) == {"step", "loss"}


def test_hint_required_columns_wants_no_metrics_when_only_hparams_are_curated() -> None:
    """Metrics and hparams are fetched independently -- curating one but leaving the other at its
    default shouldn't force a full "every metric in the experiment" fetch for a table that isn't
    even set up to show any."""
    settings = TableChartSettings(hparams=["lr"])
    assert TableChart.hint_required_columns(settings) == set()


def test_hint_required_hparams_defaults_to_unbounded() -> None:
    assert TableChart.hint_required_hparams(TableChartSettings()) is None


def test_hint_required_hparams_respects_hparams_filter() -> None:
    settings = TableChartSettings(hparams=["lr", "batch_size"])
    assert TableChart.hint_required_hparams(settings) == {"lr", "batch_size"}


def test_hint_required_hparams_empty_in_pivot_mode() -> None:
    settings = TableChartSettings(pivot_on="step", pivot_metric="loss")
    assert TableChart.hint_required_hparams(settings) == set()


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


def test_render_run_mode_strips_hparam_prefix_from_column_names() -> None:
    table = TableChart.render(TableChartSettings(), _wide_df())
    rows = _rows_by_run(table)

    assert rows[1]["lr"] == pytest.approx(0.1)
    assert not any(col.startswith(HPARAM_COLUMN_PREFIX) for col in rows[1])


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

    assert "lr" in rows[1]
    assert "batch_size" not in rows[1]


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


def test_render_run_mode_excludes_the_run_name_column() -> None:
    """A run's display name (merged in for charts that label by name, see `RUN_NAME_COLUMN`) isn't
    a metric value either -- it shouldn't leak into the table as a bogus column."""
    df = _wide_df()
    df[RUN_NAME_COLUMN] = ["one", "one", "two", "two"]
    table = TableChart.render(TableChartSettings(), df)
    rows = _rows_by_run(table)

    assert RUN_NAME_COLUMN not in rows[1]


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
