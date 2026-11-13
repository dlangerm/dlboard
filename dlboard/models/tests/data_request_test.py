"""What a chart's windows of artifacts read: which of the steps a key logged, by run."""

from __future__ import annotations

import pytest

from dlboard.models import ArtifactWindow, WindowAt, thinned


@pytest.mark.parametrize(
    ("logged", "most", "kept"),
    [
        (list(range(5)), 8, list(range(5))),
        (list(range(10)), 4, [0, 4, 8, 9]),
        (list(range(11)), 4, [0, 4, 8, 10]),
        (list(range(0, 100, 10)), 3, [0, 40, 80, 90]),
    ],
    ids=["few-enough", "every-4th-and-the-last", "the-last-is-its-own", "by-position-not-value"],
)
def test_long_data_is_thinned_by_a_power_of_two_keeping_the_latest(
    logged: list[int], most: int, kept: list[int]
) -> None:
    assert thinned(logged, most) == kept


def test_the_steps_kept_stay_put_as_a_run_logs_more() -> None:
    before, after = thinned(list(range(20)), 8), thinned(list(range(24)), 8)

    assert before[:-1] == after[: len(before) - 1]


@pytest.mark.parametrize(
    ("window", "selected"),
    [
        (ArtifactWindow("k", 3), {1: {0, 10}, 2: {5}}),
        (ArtifactWindow("k", 1, at=WindowAt.LATEST), {1: {30}, 2: {25}}),
        (ArtifactWindow("k", 2, at=WindowAt.THINNED), {1: {0, 20, 30}, 2: {5, 25}}),
    ],
    ids=["the-first-steps-of-any-run", "each-runs-latest", "each-run-thinned"],
)
def test_a_window_reads_the_steps_it_says_of_the_ones_each_run_logged(
    window: ArtifactWindow, selected: dict[int, set[int]]
) -> None:
    assert window.selected({1: [0, 10, 20, 30], 2: [5, 15, 25]}) == selected
