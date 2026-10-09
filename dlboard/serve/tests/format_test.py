"""Tests for the shared value formatting helpers."""

from __future__ import annotations

import pytest

from dlboard.serve._format import format_duration


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0s"),
        (59, "59s"),
        (59.9, "59s"),
        (60, "1m 0s"),
        (203, "3m 23s"),
        (3599, "59m 59s"),
        (3600, "1h 0m"),
        (3725, "1h 2m"),
        (86399, "23h 59m"),
        (86400, "1d 0h"),
        (90061, "1d 1h"),
        (30 * 86400 + 5 * 3600, "30d 5h"),
        (-5, "0s"),
    ],
)
def test_a_duration_reads_as_its_two_largest_units(seconds: float, text: str) -> None:
    assert format_duration(seconds) == text
