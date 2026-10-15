"""Tests for the histogram model's write-side validation and how it rides a metrics batch."""

from __future__ import annotations

from contextlib import nullcontext

import pendulum
import pytest
from pydantic import ValidationError

from dlboard.models import Histogram, LoggedMetrics, MetricColumn, max_histogram_buckets

_TS = pendulum.datetime(2026, 1, 1, tz="UTC")
LIMIT_ENV_VAR = "DLBOARD_MAX_HISTOGRAM_BUCKETS"


@pytest.mark.parametrize(
    ("edges", "counts", "match"),
    [
        ([0.0, 1.0, 2.0], [1], "1 counts need 2 edges, got 3"),
        ([0.0, 1.0, 1.0], [1, 1], "strictly increasing"),
        ([0.0, 2.0, 1.0], [1, 1], "strictly increasing"),
        ([0.0, 1.0], [-1], "greater than or equal to 0"),
        ([0.0, float("inf")], [1], "finite"),
        ([0.0], [], "at least 2"),
    ],
)
def test_histogram_rejects_a_malformed_shape(edges: list[float], counts: list[int], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        Histogram(edges=edges, counts=counts)


@pytest.mark.parametrize(
    ("env_limit", "buckets", "fits"), [(None, 1024, True), (None, 1025, False), (8, 8, True), (8, 9, False)]
)
def test_histogram_buckets_are_limited_by_the_environment(
    monkeypatch: pytest.MonkeyPatch, env_limit: int | None, buckets: int, fits: bool
) -> None:
    monkeypatch.delenv(LIMIT_ENV_VAR, raising=False)
    if env_limit is not None:
        monkeypatch.setenv(LIMIT_ENV_VAR, str(env_limit))
    max_histogram_buckets.cache_clear()
    try:
        with nullcontext() if fits else pytest.raises(ValidationError, match="at most"):
            Histogram(edges=list(range(buckets + 1)), counts=[1] * buckets)
    finally:
        max_histogram_buckets.cache_clear()


def test_logged_metrics_carries_histograms_to_table_entries() -> None:
    histogram = Histogram(edges=[0.0, 1.0, 4.0], counts=[3, 5], nonfinite=2)
    logged = LoggedMetrics(
        metrics={"loss": 1.0},
        histograms={"grad": histogram},
        step=7,
        experiment_id=1,
        run_id=2,
        timestamp_utc=_TS,
    )

    (entry,) = logged.to_underlying_histograms()

    assert (entry.key, entry.experiment_id, entry.run_id, entry.step) == ("grad", 1, 2, 7)
    assert entry.histogram == histogram
    assert [m.key for m in logged.to_underlying()] == ["loss"]


@pytest.mark.parametrize("reserved", list(MetricColumn))
def test_logged_metrics_rejects_a_histogram_named_like_a_fixed_column(reserved: MetricColumn) -> None:
    histogram = Histogram(edges=[0.0, 1.0], counts=[1])
    with pytest.raises(ValidationError, match="reserved column names"):
        LoggedMetrics(histograms={reserved: histogram}, step=0, experiment_id=1, run_id=1, timestamp_utc=_TS)


def test_logged_metrics_from_an_old_client_has_no_histograms() -> None:
    logged = LoggedMetrics.model_validate(
        {"metrics": {"loss": 1.0}, "step": 0, "experiment_id": 1, "run_id": 1, "timestamp_utc": _TS}
    )
    assert logged.histograms == {}
