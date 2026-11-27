"""`Backoff`: a failed work item's first wait, doubled with each try after, never past the most it may wait."""

from __future__ import annotations

import pendulum
from hypothesis import given
from hypothesis import strategies as st

from dlboard.serve._custodian._task import Backoff

_SECONDS = st.integers(1, 10_000)


@given(first=_SECONDS, most=_SECONDS, attempts=st.integers(1, 200))
def test_each_wait_is_twice_the_one_before_until_it_reaches_the_most(
    first: int, most: int, attempts: int
) -> None:
    backoff = Backoff(pendulum.duration(seconds=first), pendulum.duration(seconds=most))

    assert backoff.after(1) == min(backoff.first, backoff.most)
    assert backoff.after(attempts + 1) == min(backoff.after(attempts) * 2, backoff.most)
