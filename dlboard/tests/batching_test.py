"""Tests for `ship_batches`, the batch-and-retry loop the client shippers and the artifact ref ingest share."""

from __future__ import annotations

import pytest

from dlboard import _batching
from dlboard._batching import BatchParams, ship_batches
from dlboard._mp_context import SPAWN_CONTEXT


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


def test_ship_batches_backs_off_exponentially_and_resets_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each consecutive transient failure waits longer than the last, reset the moment one ships."""
    slept: list[float] = []

    def no_jitter(low: float, high: float) -> float:
        del low, high
        return 0.0

    monkeypatch.setattr(_batching.time, "sleep", slept.append)
    monkeypatch.setattr(_batching.random, "uniform", no_jitter)

    # Fails 3 times, succeeds, fails once more (backoff must restart from the base), then succeeds
    # again -- the last success is the flush sentinel's own retry of what the 5th attempt left behind.
    outcomes: list[Exception | None] = [
        RuntimeError(),
        RuntimeError(),
        RuntimeError(),
        None,
        RuntimeError(),
        None,
    ]

    def ship(batch: list[str]) -> None:
        del batch
        if outcome := outcomes.pop(0):
            raise outcome

    params = BatchParams(flush_size=1, wait_sec=1, ready=SPAWN_CONTEXT.Event(), flushed=SPAWN_CONTEXT.Event())
    with pytest.raises(_ScriptDone):
        ship_batches(
            _ScriptedQueue(["a", "a", "a", "a", "a", None]),
            ship,
            params,
            is_permanent=lambda _exc: False,
        )

    assert slept == [1, 2, 4, 1]


def test_ship_batches_splits_an_oversized_batch_and_ships_both_halves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch too large for one request is split in half; each half ships once both fit."""

    def no_sleep(seconds: float) -> None:
        del seconds

    monkeypatch.setattr(_batching.time, "sleep", no_sleep)

    class _TooLargeError(Exception):
        pass

    successes: list[list[str]] = []

    def ship(batch: list[str]) -> None:
        if len(batch) > 1:
            raise _TooLargeError
        successes.append(list(batch))

    # A large `wait_sec`, so only `flush_size` being reached (not the batch going stale) triggers a
    # ship attempt -- isolates the splitting behavior from the unrelated staleness-based trigger.
    params = BatchParams(
        flush_size=4, wait_sec=100, ready=SPAWN_CONTEXT.Event(), flushed=SPAWN_CONTEXT.Event()
    )
    with pytest.raises(_ScriptDone):
        ship_batches(
            _ScriptedQueue(["a", "b", "c", "d", None]),
            ship,
            params,
            is_permanent=lambda _exc: False,
            is_oversized=lambda exc: isinstance(exc, _TooLargeError),
        )

    assert sorted(successes) == [["a"], ["b"], ["c"], ["d"]]


def test_ship_batches_drops_a_single_item_still_too_large_to_split(monkeypatch: pytest.MonkeyPatch) -> None:
    """One item alone over the limit can never succeed by splitting further -- it's dropped, not retried forever."""

    def no_sleep(seconds: float) -> None:
        del seconds

    monkeypatch.setattr(_batching.time, "sleep", no_sleep)

    class _TooLargeError(Exception):
        pass

    successes: list[list[str]] = []

    def ship(batch: list[str]) -> None:
        if "huge" in batch:
            raise _TooLargeError
        successes.append(list(batch))

    params = BatchParams(
        flush_size=2, wait_sec=100, ready=SPAWN_CONTEXT.Event(), flushed=SPAWN_CONTEXT.Event()
    )
    with pytest.raises(_ScriptDone):
        ship_batches(
            _ScriptedQueue(["huge", "fine", None]),
            ship,
            params,
            is_permanent=lambda exc: isinstance(exc, _TooLargeError),
            is_oversized=lambda exc: isinstance(exc, _TooLargeError),
        )

    assert successes == [["fine"]]
