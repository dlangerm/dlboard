"""
`--shard=I/N` (#815): CI's browser job runs as N jobs side by side, each a shard of the tests of about the same time.

Every test is in exactly one shard, a test with no recorded duration too, and no shard takes longer than another by more
than the longest test: each, longest first, goes to the shard with the least so far.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dlboard.conftest import Shard, split_into_shards

_TESTS = st.dictionaries(
    st.text("abc:/", min_size=1, max_size=4), st.one_of(st.none(), st.floats(0, 60)), max_size=30
)
"""Test ids, each with its recorded duration, or `None` for one never recorded."""


@given(tests=_TESTS, count=st.integers(1, 4))
def test_every_test_is_in_one_shard_and_no_shard_is_longer_than_another_by_more_than_one_test(
    tests: dict[str, float | None], count: int
) -> None:
    durations = {nodeid: seconds for nodeid, seconds in tests.items() if seconds is not None}

    shards = split_into_shards(durations, tests, count)

    assert len(shards) == count
    assert sorted(nodeid for shard in shards for nodeid in shard) == sorted(tests)
    loads = [sum(durations.get(nodeid, 0.0) for nodeid in shard) for shard in shards]
    assert max(loads) - min(loads) <= max(durations.values(), default=0.0) + 1e-9


@pytest.mark.parametrize(("text", "shard"), [("1/3", Shard(1, 3)), ("3/3", Shard(3, 3))])
def test_a_shard_is_given_as_its_number_of_how_many(text: str, shard: Shard) -> None:
    assert Shard.parse(text) == shard


@pytest.mark.parametrize("text", ["0/3", "4/3", "1", "a/b"])
def test_a_shard_that_is_not_one_of_them_is_refused(text: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 -- int() and the range check both say so as a ValueError
        Shard.parse(text)
