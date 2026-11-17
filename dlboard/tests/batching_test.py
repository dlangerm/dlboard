"""Tests for `ship_batches`, the batch-and-retry loop the client shippers and the artifact ref ingest share."""

from __future__ import annotations

import pytest

from dlboard import _batching
from dlboard._batching import BatchParams, Failure, ship_batches
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


class _RefusedError(Exception):
    pass


def _transient(_exc: Exception) -> Failure:
    return Failure.TRANSIENT


def _no_sleep(seconds: float) -> None:
    del seconds


@pytest.mark.parametrize(
    ("failure", "shipped", "dropped"),
    [
        # Transient: kept and retried, with what arrived meanwhile, so nothing is lost.
        (Failure.TRANSIENT, [["a", "bad", "c", "d"]], []),
        # Per item: split until the bad item is alone, so it costs only itself, and nothing is sent twice.
        (Failure.PER_ITEM, [["a"], ["c"], ["d"]], [1]),
        # Whole: nothing in it can land, so it's dropped -- without wedging what's queued after it.
        (Failure.WHOLE, [["d"]], [3]),
    ],
)
def test_ship_batches_keeps_splits_or_drops_a_failed_batch_as_its_failure_says(
    monkeypatch: pytest.MonkeyPatch, failure: Failure, shipped: list[list[str]], dropped: list[int]
) -> None:
    monkeypatch.setattr(_batching.time, "sleep", _no_sleep)
    attempts: list[list[str]] = []
    successes: list[list[str]] = []
    drops: list[int] = []

    def ship(batch: list[str]) -> None:
        attempts.append(list(batch))
        if "bad" in batch and (failure is not Failure.TRANSIENT or len(attempts) == 1):
            raise _RefusedError
        successes.append(list(batch))

    def classify(_exc: Exception) -> Failure:
        return failure

    # A large `wait_sec`, so only a flush (`None`) ships: the batches are exactly what was queued between them.
    flushed = SPAWN_CONTEXT.Event()
    params = BatchParams(flush_size=100, wait_sec=100, ready=SPAWN_CONTEXT.Event(), flushed=flushed)
    with pytest.raises(_ScriptDone):
        ship_batches(
            _ScriptedQueue(["a", "bad", "c", None, "d", None]),
            ship,
            params,
            classify=classify,
            on_drop=drops.append,
        )

    assert successes == shipped
    assert drops == dropped
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
        ship_batches(_ScriptedQueue(["a", "a", "a", "a", "a", None]), ship, params, classify=_transient)

    assert slept == [1, 2, 4, 1]


def test_ship_batches_runs_on_flush_after_the_shipping_and_before_the_flush_is_acknowledged() -> None:
    """What a process holds that dies with it (unexported spans) is pushed out before the parent is told it's safe."""
    flushed = SPAWN_CONTEXT.Event()
    events: list[str] = []

    def on_flush() -> None:
        events.append(f"on_flush (acknowledged: {flushed.is_set()})")

    params = BatchParams(flush_size=100, wait_sec=0, ready=SPAWN_CONTEXT.Event(), flushed=flushed)
    with pytest.raises(_ScriptDone):
        ship_batches(
            _ScriptedQueue(["a", None]),
            lambda batch: events.append(f"ship {batch}"),
            params,
            classify=_transient,
            on_flush=on_flush,
        )

    assert events == ["ship ['a']", "on_flush (acknowledged: False)"]
    assert flushed.is_set()
