"""
Drain a queue into batches and ship each one somewhere that can fail.

Shared by the client's metric/artifact shipping processes (`dlboard_logger.py`) and every blob
artifact store's ref-ingest thread (`plugins/data_stores/_blob_store.py`) -- both batch items off a
queue towards a destination that can fail transiently (a server, a locked database), and both must
never lose a batch to a failure that a retry would have survived.
"""

from __future__ import annotations

import logging
import random
import time
from queue import Empty
from typing import TYPE_CHECKING, NamedTuple, Protocol, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.synchronize import Event as MPEvent

_log = logging.getLogger(__name__)

T = TypeVar("T")
"""Pre-3.12 `TypeVar` style (the client's floor is 3.10): the kind of item `ship_batches` moves."""

_T_co = TypeVar("_T_co", covariant=True)
"""`Receiver`'s own type parameter -- covariant, since it only ever appears in a return position."""

_MAX_BACKOFF_FACTOR: int = 6
"""Caps retry backoff at `wait_sec * 2**6` (64x) -- a dead server shouldn't make the next retry wait
arbitrarily long, just long enough to stop hammering it."""

_JITTER_FRACTION: float = 0.25
"""+/- this fraction of the computed backoff, so many shipping processes retrying the same outage
don't all land on the server in lockstep."""


class BatchParams(NamedTuple):
    """How `ship_batches` batches, plus the events it reports through."""

    flush_size: int
    wait_sec: float
    ready: MPEvent
    flushed: MPEvent


class Receiver(Protocol[_T_co]):
    """The one queue method `ship_batches` needs -- both `multiprocessing` and `queue` queues have it."""

    def get(self, block: bool = True, timeout: float | None = None) -> _T_co: ...  # noqa: FBT001, FBT002


def _split_oversized(
    ship: Callable[[list[T]], None],
    batch: list[T],
    *,
    is_permanent: Callable[[Exception], bool],
    is_oversized: Callable[[Exception], bool],
) -> list[T]:
    """
    Ship `batch` in two halves, recursing into a half that's still too large, down to one item each.

    Returns whatever's left unshipped: a permanently-rejected chunk is dropped (and so absent from
    the result), but one that fails for some other transient reason comes back in it, for
    `ship_batches` to retry (at its own pace, with backoff) on a later pass -- never lost, and never
    re-sent on top of a chunk that already landed. A chunk of exactly one item that's still too
    large has nothing left to split into, so it's treated as permanent either way: resending the
    same single oversized item can never succeed.
    """
    mid = len(batch) // 2
    leftover: list[T] = []
    for chunk in (batch[:mid], batch[mid:]):
        try:
            ship(chunk)
        except Exception as exc:  # noqa: BLE001 -- nothing may kill the shipping loop
            if is_oversized(exc) and len(chunk) > 1:
                leftover.extend(
                    _split_oversized(ship, chunk, is_permanent=is_permanent, is_oversized=is_oversized)
                )
            elif is_permanent(exc):
                _log.exception("A sub-batch of %s was rejected, dropping it", len(chunk))
            else:
                leftover.extend(chunk)  # transient; retry (with backoff) on a later pass
    return leftover


def ship_batches(
    q: Receiver[T | None],
    ship: Callable[[list[T]], None],
    params: BatchParams,
    *,
    is_permanent: Callable[[Exception], bool],
    is_oversized: Callable[[Exception], bool] = lambda _exc: False,
) -> None:
    """
    Forever drain `q` into batches, shipping one once it's full, `wait_sec` old, or a flush asks for it.

    A batch whose failure `is_permanent` is dropped: resending it can never succeed, and keeping it
    would wedge every later item behind it. A batch whose failure `is_oversized` is instead split in
    half and each half attempted once right away (`_split_oversized`) -- resending the exact same
    oversized batch could never succeed either, but a batch that only grew this large because the
    server was briefly unreachable shouldn't be dropped just because it doesn't fit in one request
    once it's reachable again. Any other failure is transient -- the batch is kept, keeps growing
    with whatever arrives meanwhile, and is retried after an exponentially growing backoff (reset
    the moment a batch ships successfully) rather than hammering a server that's still down. A
    `None` queued in place of an item requests a flush, acknowledged (`params.flushed`) only once
    everything queued before it is gone. Returns once `q` itself is closed (e.g. at interpreter
    shutdown) -- nothing more can ever arrive through it.
    """
    params.ready.set()
    batch: list[T] = []
    flush_requested = False
    last_attempt = time.perf_counter()
    consecutive_failures = 0
    while True:
        try:
            item = q.get(timeout=params.wait_sec / 10)
            if item is None:
                flush_requested = True
            else:
                batch.append(item)
        except Empty:
            pass
        except (OSError, ValueError):  # a closed `multiprocessing` queue raises one or the other
            return
        due = flush_requested or len(batch) >= params.flush_size
        if batch and (due or time.perf_counter() - last_attempt > params.wait_sec):
            try:
                ship(batch)
                batch = []
                consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001 -- nothing may kill the shipping loop
                if is_oversized(exc) and len(batch) > 1:
                    _log.warning("A batch of %s was too large for one request, splitting it", len(batch))
                    batch = _split_oversized(
                        ship, batch, is_permanent=is_permanent, is_oversized=is_oversized
                    )
                    consecutive_failures = 0
                elif is_permanent(exc):
                    _log.exception("A batch of %s was rejected, dropping it", len(batch))
                    batch = []
                    consecutive_failures = 0
                else:
                    _log.exception("Failed to ship a batch of %s, retrying", len(batch))
                    backoff = params.wait_sec * 2 ** min(consecutive_failures, _MAX_BACKOFF_FACTOR)
                    jitter = backoff * _JITTER_FRACTION
                    time.sleep(backoff + random.uniform(-jitter, jitter))
                    consecutive_failures += 1
            last_attempt = time.perf_counter()
        if flush_requested and not batch:
            flush_requested = False
            params.flushed.set()
