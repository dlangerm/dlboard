"""
Drain a queue into batches and ship each one somewhere that can fail.

Shared by the client's metric/artifact shipping processes (`dltrack_logger.py`) and every blob
artifact store's ref-ingest thread (`plugins/data_stores/_blob_store.py`) -- both batch items off a
queue towards a destination that can fail transiently (a server, a locked database), and both must
never lose a batch to a failure that a retry would have survived.
"""

from __future__ import annotations

import logging
import time
from queue import Empty
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.synchronize import Event as MPEvent

_log = logging.getLogger(__name__)


class BatchParams(NamedTuple):
    """How `ship_batches` batches, plus the events it reports through."""

    flush_size: int
    wait_sec: float
    ready: MPEvent
    flushed: MPEvent


class Receiver[T](Protocol):
    """The one queue method `ship_batches` needs -- both `multiprocessing` and `queue` queues have it."""

    def get(self, block: bool = True, timeout: float | None = None) -> T: ...  # noqa: FBT001, FBT002


def ship_batches[T](
    q: Receiver[T | None],
    ship: Callable[[list[T]], None],
    params: BatchParams,
    *,
    is_permanent: Callable[[Exception], bool],
) -> None:
    """
    Forever drain `q` into batches, shipping one once it's full, `wait_sec` old, or a flush asks for it.

    A batch whose failure `is_permanent` is dropped: resending it can never succeed, and keeping it
    would wedge every later item behind it. Any other failure is transient -- the batch is kept,
    keeps growing with whatever arrives meanwhile, and is retried after `wait_sec`. A `None` queued in
    place of an item requests a flush, acknowledged (`params.flushed`) only once everything queued
    before it is gone. Returns once `q` itself is closed (e.g. at interpreter shutdown) -- nothing
    more can ever arrive through it.
    """
    params.ready.set()
    batch: list[T] = []
    flush_requested = False
    last_attempt = time.perf_counter()
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
            except Exception as exc:  # noqa: BLE001 -- nothing may kill the shipping loop
                if is_permanent(exc):
                    _log.exception("A batch of %s was rejected, dropping it", len(batch))
                    batch = []
                else:
                    _log.exception("Failed to ship a batch of %s, retrying", len(batch))
                    time.sleep(params.wait_sec)
            last_attempt = time.perf_counter()
        if flush_requested and not batch:
            flush_requested = False
            params.flushed.set()
