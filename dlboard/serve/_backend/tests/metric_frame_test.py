"""Tests for `MetricFrame`'s invariants -- split out of `models/tests/metric_test.py` with the type itself."""

from __future__ import annotations

import pendulum
import pytest

from dlboard.models import MetricColumn
from dlboard.serve._backend._metric_frame import MetricFrame, MetricRow

_TS = pendulum.datetime(2026, 1, 1, tz="UTC")


def test_metric_frame_has_fixed_dtypes_even_for_all_null_metrics() -> None:
    frame = MetricFrame.from_rows([MetricRow(1, 0, "loss", None, "2026-01-01T00:00:00Z")])

    dtypes = {column: str(dtype) for column, dtype in frame.to_frame().dtypes.items()}
    assert dtypes[MetricColumn.RUN_ID] == dtypes[MetricColumn.STEP] == "int64"
    assert dtypes["loss"] == "float64"
    assert frame.timestamp_utc.iloc[0] == _TS


def test_metric_frame_from_no_rows_is_empty_but_keeps_its_fixed_columns() -> None:
    frame = MetricFrame.from_rows([])

    assert frame.keys == frozenset()
    assert set(frame.to_frame().columns) == set(MetricColumn)


def test_merged_frames_have_a_row_wherever_any_has_one_and_each_keeps_its_own_key() -> None:
    later = "2026-01-01T00:00:09Z"
    latest = MetricFrame.from_rows([MetricRow(1, 4, "loss", 0.5, later)])
    history = MetricFrame.from_rows(
        [
            MetricRow(run, step, "acc", step / 10, "2026-01-01T00:00:00Z")
            for run in (1, 2)
            for step in range(5)
        ]
    )

    merged = MetricFrame.merged([latest, history])
    frame = merged.to_frame()

    assert merged.keys == {"loss", "acc"}
    assert [(run, step) for run, step in zip(frame.run_id, frame.step, strict=True)] == [
        (run, step) for run in (1, 2) for step in range(5)
    ]
    assert frame.loss.isna().tolist() == [True] * 4 + [False] + [True] * 5
    assert list(frame.acc) == [0.0, 0.1, 0.2, 0.3, 0.4] * 2
    assert str(frame.loss.dtype) == str(frame.acc.dtype) == "float64"
    # Where both have the row, it is stamped with the earliest of their times.
    assert frame.timestamp_utc.iloc[4] == _TS


def test_merging_frames_that_share_a_key_is_an_error_not_a_silent_pick() -> None:
    first = MetricFrame.from_rows([MetricRow(1, 0, "loss", 1.0, "2026-01-01T00:00:00Z")])
    second = MetricFrame.from_rows([MetricRow(1, 0, "loss", 2.0, "2026-01-01T00:00:00Z")])

    with pytest.raises(ValueError, match="loss"):
        MetricFrame.merged([first, second])


def test_merging_one_frame_is_that_frame() -> None:
    frame = MetricFrame.from_rows([MetricRow(1, 0, "loss", 1.0, "2026-01-01T00:00:00Z")])

    assert MetricFrame.merged([frame]) is frame
    assert MetricFrame.merged([]).keys == frozenset()


def test_latest_per_run_is_each_keys_last_number_in_the_run() -> None:
    frame = MetricFrame.from_rows(
        [
            MetricRow(1, 0, "loss", 3.0, "2026-01-01T00:00:00Z"),
            MetricRow(1, 1, "loss", 2.0, "2026-01-01T00:00:01Z"),
            MetricRow(1, 2, "loss", None, "2026-01-01T00:00:02Z"),
            MetricRow(1, 0, "acc", None, "2026-01-01T00:00:00Z"),
            MetricRow(2, 0, "loss", 9.0, "2026-01-01T00:00:00Z"),
        ]
    )

    assert frame.latest_per_run() == {1: {"loss": 2.0}, 2: {"loss": 9.0}}
