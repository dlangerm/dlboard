"""Tests for `ship_batches`, the batch-and-retry loop the client shippers and the artifact ref ingest share."""

from __future__ import annotations

import pytest

from dltrack._batching import BatchParams, ship_batches
from dltrack._mp_context import SPAWN_CONTEXT


class _ScriptDone(BaseException):
    """Ends `ship_batches`' endless loop once `_ScriptedQueue` runs dry (not an `Exception`, so it isn't retried)."""


class _ScriptedQueue:
    def __init__(self, items: list[str | None]) -> None:
        self._items = items

    def get(self, block: bool = True, timeout: float | None = None) -> str | None:
        del block, timeout
        if not self._items:
            raise _ScriptDone
        return self._items.pop(0)


@pytest.mark.parametrize(
    ("failure", "shipped"),
    [
        # Permanent: dropped, and it doesn't wedge the items queued after it.
        (ValueError("never going to work"), [["b"]]),
        # Transient: kept and retried, so nothing is lost.
        (RuntimeError("try again"), [["a"], ["b"]]),
    ],
)
def test_ship_batches_drops_permanent_failures_and_retries_transient_ones(
    failure: Exception, shipped: list[list[str]]
) -> None:
    failures = [failure]
    successes: list[list[str]] = []

    def ship(batch: list[str]) -> None:
        if failures:
            raise failures.pop()
        successes.append(list(batch))

    flushed = SPAWN_CONTEXT.Event()
    params = BatchParams(flush_size=100, wait_sec=0, ready=SPAWN_CONTEXT.Event(), flushed=flushed)
    with pytest.raises(_ScriptDone):
        ship_batches(
            _ScriptedQueue(["a", None, "b", None]),
            ship,
            params,
            is_permanent=lambda exc: isinstance(exc, ValueError),
        )

    assert successes == shipped
    assert flushed.is_set()
