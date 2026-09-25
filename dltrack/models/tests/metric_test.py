"""Tests for the metric models: write-side validation and `MetricFrame`'s invariants."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from dltrack.models import LoggedMetrics, MetricColumn, MetricFrame, MetricRow

_TS = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("reserved", list(MetricColumn))
def test_logged_metrics_rejects_a_key_named_like_a_fixed_column(reserved: MetricColumn) -> None:
    """Such a metric used to be silently overwritten by the fixed column of the same name."""
    with pytest.raises(ValidationError, match="reserved column names"):
        LoggedMetrics(metrics={reserved: 1.0}, step=0, experiment_id=1, run_id=1, timestamp_utc=_TS)


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
