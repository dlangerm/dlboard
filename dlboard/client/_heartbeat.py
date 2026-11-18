"""
A run's heartbeat: a word to the server every minute that the run is alive, between the batches it ships.

A run's batches already say so, a few seconds apart; this covers the stretches with none (a long evaluation, a stalled
data loader), so the server gives up as lost only a run whose process has really gone (`DLBOARD_LOST_RUN_AFTER_S`, ten
times as long by default). On its own thread, like the system metrics sampler: a beat that fails, for whatever reason, is
tried again at the next one, and a server that has no heartbeat (a 404) is not asked again.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Final

import requests
from structlog.stdlib import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

_log = get_logger(__name__)

BEAT_EVERY_SEC: Final = 60.0


class Heartbeat:
    """Calls `beat` every `interval_s`, until it says the server isn't listening or the run stops it."""

    def __init__(self, beat: Callable[[], bool], interval_s: float = BEAT_EVERY_SEC) -> None:
        self._beat, self._interval_s = beat, interval_s
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._beat_until_stopped, name="dlboard-heartbeat", daemon=True
        )

    @classmethod
    def start(cls, beat: Callable[[], bool], interval_s: float = BEAT_EVERY_SEC) -> Heartbeat:
        heartbeat = cls(beat, interval_s)
        heartbeat._thread.start()
        return heartbeat

    def _beat_until_stopped(self) -> None:
        while not self._stopped.wait(self._interval_s):
            try:
                if not self._beat():
                    return
            # The server or network is down: the next beat may get through.
            except requests.RequestException as exc:
                _log.debug("A run's heartbeat did not reach the server", reason=str(exc))
            except Exception:  # noqa: BLE001 -- a heartbeat must never halt training, nor stop beating for it
                _log.warning("A run's heartbeat failed; the next one is tried as usual", exc_info=True)

    def stop(self) -> None:
        """Beat no more."""
        self._stopped.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(1.0)
