"""`test-durations.json` lists exactly the tests the suite has, so every test is tiered by a real measurement."""

from __future__ import annotations

import pytest

from dlboard.conftest import COLLECTED_TESTS, load_durations


def test_recorded_durations_match_the_collected_suite(request: pytest.FixtureRequest) -> None:
    collected = request.config.stash.get(COLLECTED_TESTS, None)
    if collected is None:
        pytest.skip("only a run that collects the whole suite (no paths; -k/-m are fine) knows every test")
    recorded = set(load_durations())

    assert sorted(recorded - collected) == [], (
        "test-durations.json lists tests that no longer exist; a `uv run pytest --record-durations` "
        "run with no paths drops them"
    )
    assert sorted(collected - recorded) == [], (
        "test-durations.json has no duration for these tests; record them with "
        "`uv run pytest <their files> --record-durations`"
    )
