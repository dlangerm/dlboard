"""Tests for `MetricFrame`'s invariants -- split out of `models/tests/metric_test.py` with the type itself."""

from __future__ import annotations

from datetime import UTC, datetime

from dlboard.models import MetricColumn
from dlboard.serve._backend._metric_frame import MetricFrame, MetricRow

_TS = datetime(2026, 1, 1, tzinfo=UTC)


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
    assert frame.latest_per_run() == {}
