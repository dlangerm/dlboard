"""
A run's console capture: what is written reaches the real stream and a chunk, without ever waiting on either.

Each capture is started in the test itself: pytest points `sys.stdout` at its own capture again between a fixture and
the test, so one started by a fixture would never see the test's prints.
"""

from __future__ import annotations

import sys
import threading

import pytest

from dlboard.client._console import (
    CAPPED_TAG,
    DROPPED_TAG,
    ConsoleCapture,
    ConsoleChunk,
    ConsoleSettings,
    collapse_carriage_returns,
)


@pytest.mark.parametrize(
    ("written", "kept"),
    [
        ("plain\nlines\n", "plain\nlines\n"),
        ("10%\r50%\r100%\ndone\n", "100%\ndone\n"),
        ("epoch 1\r\n", "epoch 1\n"),  # a Windows line end is not an update
        ("redrawn\r", "redrawn"),
    ],
)
def test_a_progress_bar_is_kept_as_it_last_looked(written: str, kept: str) -> None:
    assert collapse_carriage_returns(written) == kept


@pytest.fixture
def chunks() -> list[ConsoleChunk]:
    return []


def _capture(chunks: list[ConsoleChunk], *, max_mb: int = 10, chunk_kb: int = 256) -> ConsoleCapture:
    capture = ConsoleCapture.start(
        ConsoleSettings(enabled=True, interval_s=3600, max_mb=max_mb, chunk_kb=chunk_kb), chunks.append
    )
    assert capture is not None
    return capture


def test_what_is_printed_reaches_the_next_chunk_in_order(chunks: list[ConsoleChunk]) -> None:
    capture = _capture(chunks)
    print("step 1")
    sys.stderr.write("Traceback (most recent call last):\n")
    sys.stderr.writelines(
        ['  File "train.py", line 3\n', "ZeroDivisionError\n"]
    )  # how `traceback` writes some
    print("step 2")

    capture.stop()

    (chunk,) = chunks
    assert chunk.data.decode() == (
        'step 1\nTraceback (most recent call last):\n  File "train.py", line 3\nZeroDivisionError\nstep 2\n'
    )
    assert (chunk.step, chunk.tags) == (0, {})


def test_stopping_cuts_the_last_chunk_and_puts_the_streams_back(chunks: list[ConsoleChunk]) -> None:
    streams = (sys.stdout, sys.stderr)
    capture = _capture(chunks)
    print("last words")

    capture.stop()
    print("after")

    assert (sys.stdout, sys.stderr) == streams
    assert [chunk.data.decode() for chunk in chunks] == ["last words\n"]


def test_a_stream_something_else_wrapped_since_is_left_to_it(chunks: list[ConsoleChunk]) -> None:
    capture = _capture(chunks)
    later = _capture(list[ConsoleChunk]())
    capture.stop()

    assert sys.stdout is later._tees[0]
    later.stop()


def test_a_write_that_finds_the_buffer_full_is_dropped_and_said_so(chunks: list[ConsoleChunk]) -> None:
    capture = _capture(chunks)
    for _ in range(5):  # four chunks' worth is held; the fifth finds the buffer full
        capture.append("x" * 256 * 1024)

    capture.stop()

    assert chunks[-1].tags == {DROPPED_TAG: 256 * 1024}


def test_past_the_cap_a_run_captures_nothing_and_its_last_chunk_says_so(chunks: list[ConsoleChunk]) -> None:
    capture = _capture(chunks, max_mb=1)
    capture.append("x" * 700 * 1024)
    capture.cut()
    capture.append("y" * 700 * 1024)
    capture.cut()
    capture.append("z")
    capture.stop()

    assert [len(chunk.data) for chunk in chunks] == [700 * 1024, 324 * 1024]
    assert chunks[-1].tags == {CAPPED_TAG: True}


def test_a_chunk_is_cut_without_waiting_for_the_interval_once_it_is_full(chunks: list[ConsoleChunk]) -> None:
    cut = threading.Event()

    def ship(chunk: ConsoleChunk) -> None:
        chunks.append(chunk)
        cut.set()

    capture = ConsoleCapture.start(ConsoleSettings(enabled=True, interval_s=3600, chunk_kb=1), ship)
    assert capture is not None

    capture.append("x" * 2048)

    assert cut.wait(10)
    capture.stop()
    assert len(chunks[0].data) == 2048


def test_a_run_captures_nothing_when_turned_off(chunks: list[ConsoleChunk]) -> None:
    streams = (sys.stdout, sys.stderr)

    assert ConsoleCapture.start(ConsoleSettings(enabled=False), chunks.append) is None
    assert (sys.stdout, sys.stderr) == streams
