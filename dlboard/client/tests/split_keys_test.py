"""A wide `log_metrics` call is queued as items of bounded width, so a shipped batch stays a bounded upload."""

from __future__ import annotations

import math

import pytest

from dlboard.client.dlboard_logger import _KEYS_PER_QUEUED_METRICS, _split_keys


@pytest.mark.parametrize(
    "keys",
    [0, 1, _KEYS_PER_QUEUED_METRICS, _KEYS_PER_QUEUED_METRICS + 1, 11 * _KEYS_PER_QUEUED_METRICS + 5],
)
def test_a_wide_call_is_queued_as_chunks_that_together_hold_every_key_in_order(keys: int) -> None:
    metrics = {f"metric_{i:05}": float(i) for i in range(keys)}

    chunks = _split_keys(metrics)

    assert len(chunks) == max(1, math.ceil(keys / _KEYS_PER_QUEUED_METRICS))
    assert all(len(chunk) <= _KEYS_PER_QUEUED_METRICS for chunk in chunks)
    assert [key for chunk in chunks for key in chunk] == list(metrics)
    assert {key: value for chunk in chunks for key, value in chunk.items()} == metrics
