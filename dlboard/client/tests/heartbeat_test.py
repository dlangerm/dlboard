"""A run's heartbeat: every interval until stopped, through any beat that fails, and never again to a server without it."""

from __future__ import annotations

import threading

import pytest
import requests

from dlboard.client._heartbeat import Heartbeat


@pytest.mark.parametrize(
    "failure",
    [requests.ConnectionError("server down"), ValueError("an answer that isn't JSON")],
    ids=["server down", "anything else"],
)
def test_it_beats_through_a_beat_that_fails_until_stopped(failure: Exception) -> None:
    beats: list[int] = []
    third = threading.Event()

    def beat() -> bool:
        beats.append(len(beats))
        if len(beats) == 1:
            raise failure
        if len(beats) == 3:
            third.set()
        return True

    heartbeat = Heartbeat.start(beat, interval_s=0.01)
    assert third.wait(10)
    heartbeat.stop()
    stopped_at = len(beats)

    assert not heartbeat._thread.is_alive()
    assert len(beats) == stopped_at


def test_a_server_that_is_not_listening_is_not_asked_again() -> None:
    beats: list[bool] = []

    def beat() -> bool:
        beats.append(False)
        return False

    heartbeat = Heartbeat.start(beat, interval_s=0.01)
    heartbeat._thread.join(10)

    assert (beats, heartbeat._thread.is_alive()) == ([False], False)
