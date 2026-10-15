"""Tests for the metric model's write-side validation."""

from __future__ import annotations

import pendulum
import pytest
from pydantic import ValidationError

from dlboard.models import LoggedMetrics, MetricColumn

_TS = pendulum.datetime(2026, 1, 1, tz="UTC")


@pytest.mark.parametrize("reserved", list(MetricColumn))
def test_logged_metrics_rejects_a_key_named_like_a_fixed_column(reserved: MetricColumn) -> None:
    """Such a metric used to be silently overwritten by the fixed column of the same name."""
    with pytest.raises(ValidationError, match="reserved column names"):
        LoggedMetrics(metrics={reserved: 1.0}, step=0, experiment_id=1, run_id=1, timestamp_utc=_TS)
