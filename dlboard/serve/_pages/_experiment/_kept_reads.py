"""The last few things a worker read, kept under a key that changes whenever what they were read from does. No Dash."""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class KeptReads[Key, Value]:
    """
    A few reads kept on a worker, so a request that asks for the same again is answered without reading it.

    The caller's key says what the read depends on, including the `revision` of each experiment it was read from -- which
    every write to it bumps -- so a read is never served after the data changed, on this worker or another. The oldest
    read goes first once more than `kept` are held.
    """

    def __init__(self, kept: int) -> None:
        self._kept = kept
        self._reads: OrderedDict[Key, Value] = OrderedDict()
        self._lock = threading.Lock()

    def read(self, key: Key, fetch: Callable[[], Value]) -> Value:
        """The read kept under `key`, or `fetch`'s, kept for the next request that asks for the same."""
        with self._lock:
            if (kept := self._reads.get(key)) is not None:
                self._reads.move_to_end(key)
                return kept
        fetched = fetch()
        with self._lock:
            self._reads[key] = fetched
            while len(self._reads) > self._kept:
                self._reads.popitem(last=False)
        return fetched
