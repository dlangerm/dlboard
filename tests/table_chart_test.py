# pyright: reportPrivateUsage=false
"""Tests for the table chart type: run-mode rows, pivot mode, and its column hints."""

from __future__ import annotations

from typing import Any, cast

import pandas as pd
import pytest

from dltrack.plugins.charts._table_style import DEFAULT_TABLE_FONT_SIZE, HPARAM_COLUMN_PREFIX
from dltrack.plugins.charts.table_chart import TableChart, TableChartSettings


def _props(component: object) -> dict[str, Any]:
    return cast("Any", component).to_plotly_json()["props"]


def _rows_by_run(component: object) -> dict[int, dict[str, Any]]:
    return {row["run_id"]: row for row in _props(component)["data"]}


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
    rows = _props(table)["data"]
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


def test_render_run_mode_excludes_artifact_tags_columns() -> None:
    df = _wide_df()
    df["img"] = ["ref://1-0", "ref://1-1", "ref://2-0", "ref://2-1"]
    df["img__tags"] = pd.Series([{"split": "train"}, {"split": "train"}, None, None], dtype=object)
    table = TableChart.render(TableChartSettings(), df)
    rows = _rows_by_run(table)

    assert "img__tags" not in rows[1]
    assert "img" in rows[1]


def test_render_defaults_to_the_shared_smaller_font_size() -> None:
    table = TableChart.render(TableChartSettings(), _wide_df())
    assert _props(table)["style_cell"]["fontSize"] == DEFAULT_TABLE_FONT_SIZE


def test_render_font_size_override_applies_in_pixels() -> None:
    table = TableChart.render(TableChartSettings(font_size=20), _wide_df())
    assert _props(table)["style_cell"]["fontSize"] == "20px"


def test_render_run_mode_without_step_column_dedupes_by_run() -> None:
    df = pd.DataFrame({"run_id": [1, 1], f"{HPARAM_COLUMN_PREFIX}lr": [0.1, 0.1]})
    table = TableChart.render(TableChartSettings(), df)
    rows = _props(table)["data"]
    assert len(rows) == 1


# ---- render: pivot mode ----


def test_render_pivot_mode_one_row_per_pivot_value_one_column_per_run() -> None:
    table = TableChart.render(TableChartSettings(pivot_on="step", pivot_metric="loss"), _wide_df())
    props = _props(table)

    assert {c["id"] for c in props["columns"]} == {"step", "1", "2"}
    row_by_step = {row["step"]: row for row in props["data"]}
    assert row_by_step[0]["1"] == pytest.approx(0.5)
    assert row_by_step[1]["2"] == pytest.approx(0.7)


def test_render_pivot_mode_without_pivot_metric_shows_a_message() -> None:
    table = TableChart.render(TableChartSettings(pivot_on="step"), _wide_df())
    rows = _props(table)["data"]
    assert len(rows) == 1
    assert "pivot_metric" in rows[0]["message"]


def test_render_pivot_mode_missing_columns_shows_a_message() -> None:
    table = TableChart.render(TableChartSettings(pivot_on="epoch", pivot_metric="loss"), _wide_df())
    rows = _props(table)["data"]
    assert len(rows) == 1
    assert "message" in rows[0]
