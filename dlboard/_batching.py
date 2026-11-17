"""
Drain a queue into batches and ship each one somewhere that can fail.

Shared by the client's metric/artifact shipping processes (`dlboard_logger.py`) and every blob
artifact store's ref-ingest thread (`plugins/data_stores/_blob_store.py`) -- both batch items off a
queue towards a destination that can fail transiently (a server, a locked database), and both must
never lose a batch to a failure that a retry would have survived.
"""

from __future__ import annotations

import enum
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


class Failure(enum.Enum):
    """What a failed shipment says about the batch it was shipping."""

    TRANSIENT = enum.auto()
    """Nothing about the batch: the destination is down or busy. Kept, and tried again after a backoff."""
    PER_ITEM = enum.auto()
    """Something in the batch (one bad item, or too many at once). Split in halves until only what fails alone is
    dropped, so one bad item costs itself and not the batch it came in."""
    WHOLE = enum.auto()
    """Nothing in the batch can ever land as sent (refused credentials, a run that is gone). Dropped whole."""


class BatchParams(NamedTuple):
    """How `ship_batches` batches, plus the events it reports through."""

    flush_size: int
    wait_sec: float
    ready: MPEvent
    flushed: MPEvent


class Receiver(Protocol[_T_co]):
    """The one queue method `ship_batches` needs -- both `multiprocessing` and `queue` queues have it."""

    def get(self, block: bool = True, timeout: float | None = None) -> _T_co: ...  # noqa: FBT001, FBT002


def _ship_what_can_land(
    ship: Callable[[list[T]], None],
    batch: list[T],
    *,
    classify: Callable[[Exception], Failure],
    on_drop: Callable[[int], None],
) -> list[T]:
    """
    Ship `batch`, returning what is left to try again later: all of it, if it failed transiently.

    A `PER_ITEM` failure ships each half the same way, down to single items, so whatever lands has landed (and is
    never sent again) and only an item that fails on its own is dropped. Everything dropped is counted to `on_drop`.
    """
    try:
        ship(batch)
    except Exception as exc:  # noqa: BLE001 -- nothing may kill the shipping loop
        match classify(exc):
            case Failure.TRANSIENT:
                _log.warning("Failed to ship a batch of %s, retrying", len(batch), exc_info=True)
                return batch
            case Failure.PER_ITEM if len(batch) > 1:
                mid = len(batch) // 2
                return [
                    item
                    for half in (batch[:mid], batch[mid:])
                    for item in _ship_what_can_land(ship, half, classify=classify, on_drop=on_drop)
                ]
            case Failure.PER_ITEM | Failure.WHOLE:
                _log.exception("A batch of %s can never be shipped, dropping it", len(batch))
                on_drop(len(batch))
    return []


def ship_batches(  # noqa: PLR0913 -- each is a distinct knob of one loop
    q: Receiver[T | None],
    ship: Callable[[list[T]], None],
    params: BatchParams,
    *,
    classify: Callable[[Exception], Failure],
    on_flush: Callable[[], None] = lambda: None,
    on_drop: Callable[[int], None] = lambda _count: None,
) -> None:
    """
    Forever drain `q` into batches, shipping one once it's full, `wait_sec` old, or a flush asks for it.

    How a failed shipment is handled is up to what `classify` makes of it (see `Failure`): a transient one keeps the
    batch, which keeps growing with whatever arrives meanwhile, and retries it after an exponentially growing backoff
    (reset the moment a batch ships) rather than hammering a destination that's still down; the others drop what can
    never land, counting it to `on_drop`, without wedging the items queued behind it. A `None` queued in place of an
    item requests a flush, acknowledged (`params.flushed`) only once everything queued before it is gone and
    `on_flush` (which must not raise) has run: whatever else the process holds that dies with it, such as spans not
    yet exported, can be pushed out first. Returns once `q` itself is closed (e.g. at interpreter shutdown) --
    nothing more can ever arrive through it.
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
            batch = _ship_what_can_land(ship, batch, classify=classify, on_drop=on_drop)
            if batch:
                # `int ** int` is typed `Any` (it's a float for a negative exponent), hence the annotation.
                backoff: float = params.wait_sec * 2 ** min(consecutive_failures, _MAX_BACKOFF_FACTOR)
                jitter = backoff * _JITTER_FRACTION
                time.sleep(backoff + random.uniform(-jitter, jitter))
                consecutive_failures += 1
            else:
                consecutive_failures = 0
            last_attempt = time.perf_counter()
        if flush_requested and not batch:
            flush_requested = False
            on_flush()
            params.flushed.set()
