"""The sign-in throttle holds only what it must: anyone can try a sign-in, and a worker's memory is not theirs to fill."""

from __future__ import annotations

from dlboard.plugins.auth import password


def test_a_username_only_tried_costs_the_throttle_nothing() -> None:
    """Regression: checking a name kept an entry for it, so a stream of made-up names grew every worker for good."""
    throttle = password._Throttle()

    for i in range(100):
        assert not throttle.locked(f"made-up-{i}")

    assert throttle._failures == {}


def test_names_whose_failures_aged_out_are_swept_once_there_are_too_many() -> None:
    throttle = password._Throttle()
    floor = password._THROTTLE_SWEEP_FLOOR
    for i in range(floor):
        throttle.fail(f"old-{i}")
    for times in throttle._failures.values():  # every one of them out of the window now
        times[:] = [t - 2 * throttle.window_seconds for t in times]

    throttle.fail("new")
    throttle.fail("newer")

    assert set(throttle._failures) == {"new", "newer"}
