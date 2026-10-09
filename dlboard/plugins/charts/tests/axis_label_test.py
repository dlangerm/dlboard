"""Tests for keeping axis titles inside their chart."""

from __future__ import annotations

import pytest

from dlboard.plugins.charts._axis_label import fit_axis_label


@pytest.mark.parametrize(
    ("label", "limit", "expected"),
    [
        ("loss", 36, "loss"),
        ("x" * 36, 36, "x" * 36),
        ("DeviceStatsMonitor.on_test_batch_end/active.all.allocated", 20, "…ctive.all.allocated"),
        ("abcdef", 4, "…def"),
    ],
)
def test_a_long_label_keeps_the_end_of_its_name_behind_an_ellipsis(
    label: str, limit: int, expected: str
) -> None:
    assert fit_axis_label(label, limit) == expected
    assert len(fit_axis_label(label, limit)) <= limit
