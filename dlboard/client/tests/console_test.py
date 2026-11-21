"""
A run's console capture: what is written reaches the real stream and a chunk, without ever waiting on either.

Each capture is started in the test itself: pytest points `sys.stdout` at its own capture again between a fixture and
the test, so one started by a fixture would never see the test's prints.
"""

from __future__ import annotations

import logging
import sys
import threading

import pytest

from dlboard.client._console import (
    CAPPED_TAG,
    DROPPED_TAG,
    ConsoleCapture,
    ConsoleChunk,
    ConsoleSettings,
    as_last_drawn,
)

_REDRAW = "\r\x1b[2K\x1b[1A\x1b[2K"
"""What rich writes before it draws a two-line progress display again: erase the line, go up one, erase that too."""


@pytest.mark.parametrize(
    ("written", "kept"),
    [
        ("plain\nlines\n", "plain\nlines\n"),
        ("10%\r50%\r100%\ndone\n", "100%\ndone\n"),
        ("epoch 1\r\n", "epoch 1\n"),  # a Windows line end is not an update
        ("redrawn\r", "redrawn"),
        # #734: rich's two lines (training and validation) were kept once for every time they were drawn.
        (
            f"Total params: 161\nEpoch 0 10%\nval 0%{_REDRAW}Epoch 0 90%\nval 50%",
            "Total params: 161\nEpoch 0 90%\nval 50%",
        ),
        # What is printed while it draws goes above it, as it does in a terminal.
        (f"Epoch 0 10%\nval 0%{_REDRAW}warning!\nEpoch 0 20%\nval 9%\n", "warning!\nEpoch 0 20%\nval 9%\n"),
        ("\x1b[1A\x1b[1Aup past the first line", "up past the first line"),
        ("\x1b[1mTotal\x1b[0m: 161\n\x1b[2A\x1b[32mgreen", "\x1b[32mgreen\n"),  # colours are text; up two
    ],
)
def test_what_is_drawn_again_is_kept_as_it_last_looked(written: str, kept: str) -> None:
    assert as_last_drawn(written) == kept


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


def test_a_chunk_ends_at_a_line_end_and_the_rest_of_the_line_goes_in_the_next(
    chunks: list[ConsoleChunk],
) -> None:
    """#734: `print` writes a line, then its end, and a chunk cut between the two split the line in two."""
    capture = _capture(chunks)
    print("step 1")
    sys.stdout.write("step 2")
    capture.cut()
    sys.stdout.write(" done\n")
    sys.stdout.write("10%\r50%")  # a line drawn in place with nothing else in a chunk goes as it looks
    capture.cut()
    capture.stop()

    assert [chunk.data.decode() for chunk in chunks] == ["step 1\n", "step 2 done\n", "50%"]


def test_stopping_cuts_the_last_chunk_and_puts_the_streams_back(chunks: list[ConsoleChunk]) -> None:
    streams = (sys.stdout, sys.stderr)
    capture = _capture(chunks)
    print("last words")

    capture.stop()
    print("after")

    assert (sys.stdout, sys.stderr) == streams
    assert [chunk.data.decode() for chunk in chunks] == ["last words\n"]


def test_a_logging_handler_made_before_the_run_is_captured_then_given_its_stream_back(
    chunks: list[ConsoleChunk],
) -> None:
    """#733: Lightning makes its handler as it is imported, so its model summary went to the real stream alone."""
    logger = logging.getLogger("dlboard.tests.console")
    handler = logging.StreamHandler()  # holds `sys.stderr` as it is now
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        capture = _capture(chunks)
        logger.info("Total params: 161")
        capture.stop()
        assert handler.stream is sys.stderr
    finally:
        logger.removeHandler(handler)

    assert [chunk.data.decode() for chunk in chunks] == ["Total params: 161\n"]


def test_a_logging_handler_that_looks_up_its_stream_as_it_writes_follows_the_capture_itself(
    chunks: list[ConsoleChunk],
) -> None:
    """Logging's last resort (and pytest's live log) reads `sys.stderr` when it writes: it can't be given a stream."""
    logger = logging.getLogger("dlboard.tests.console.follows")
    handler = logging.lastResort  # warnings and up
    assert isinstance(handler, logging.StreamHandler)
    logger.addHandler(handler)
    try:
        capture = _capture(chunks)
        logger.warning("followed")
        capture.stop()
    finally:
        logger.removeHandler(handler)

    assert [chunk.data.decode() for chunk in chunks] == ["followed\n"]


def test_a_stream_something_else_wrapped_since_is_left_to_it(chunks: list[ConsoleChunk]) -> None:
    capture = _capture(chunks)
    later = _capture(list[ConsoleChunk]())
    capture.stop()

    assert sys.stdout is later._tees[0]
    later.stop()


def test_a_write_that_finds_the_buffer_full_is_dropped_and_said_so(chunks: list[ConsoleChunk]) -> None:
    # Not started: a cutter thread woken by the first full chunk could empty the buffer between these writes.
    capture = ConsoleCapture(ConsoleSettings(enabled=True, interval_s=3600), chunks.append)
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


def test_the_cap_never_cuts_a_character_in_half(chunks: list[ConsoleChunk]) -> None:
    capture = _capture(chunks, max_mb=1)
    capture.append("x" + "é" * 600 * 1024)  # two bytes each, after one: the 1 MB cap falls inside one
    capture.stop()

    (chunk,) = chunks
    assert chunk.data.decode() == "x" + "é" * (1024 * 1024 // 2 - 1)


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
