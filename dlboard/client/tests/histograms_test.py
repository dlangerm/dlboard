"""Tests for binning values into histograms on the client."""

from __future__ import annotations

import math
from itertools import pairwise
from typing import TYPE_CHECKING

import numpy as np
import pytest
import torch
from hypothesis import example, given
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp
from pydantic import ValidationError

from dlboard.client.histograms import ExplicitBinning, ExponentialBinning

if TYPE_CHECKING:
    from dlboard.client._arrays import ArrayLike
    from dlboard.client.histograms import Binning
    from dlboard.models import Histogram

# Values over thirty orders of magnitude either side of zero, with the round ones a lattice edge lands on exactly,
# zeros, and what isn't finite.
_MAGNITUDES = st.floats(-15, 15).map(lambda exponent: 10.0**exponent) | st.sampled_from(
    [1.0, 2.0, 5.0, 10.0, 0.1, 1e-3]
)
_VALUES = hnp.arrays(
    np.float64,
    st.integers(0, 40),
    elements=st.one_of(
        st.tuples(st.sampled_from([-1.0, 1.0]), _MAGNITUDES).map(lambda signed: signed[0] * signed[1]),
        st.sampled_from([0.0, -0.0, math.nan, math.inf, -math.inf]),
    ),
)
_EXPONENTIAL = st.builds(
    ExponentialBinning,
    buckets_per_decade=st.integers(1, 20),
    zero_threshold=st.sampled_from([1e-12, 1e-4, 0.5, 3.0]),
)
_EDGES = st.lists(st.floats(-100, 100), min_size=2, max_size=6, unique=True).map(sorted)
_BINNINGS = _EXPONENTIAL | st.builds(ExplicitBinning, edges=_EDGES)


def _recount(histogram: Histogram, values: np.ndarray, *, mirrored: bool) -> list[int]:
    """
    Where `values` fall among `histogram`'s edges, counted independently of how it was binned.

    A value on an edge is in the bucket that starts there, or, `mirrored` below zero, in the one that ends there.
    """
    finite: np.ndarray = values[np.isfinite(values)]
    after = np.searchsorted(histogram.edges, finite, side="right")
    if mirrored:
        after = np.where(finite < 0, np.searchsorted(histogram.edges, finite, side="left"), after)
    index: np.ndarray = np.clip(after - 1, 0, len(histogram.counts) - 1)
    return np.bincount(index, minlength=len(histogram.counts)).tolist()


@given(_BINNINGS, _VALUES)
def test_every_finite_value_is_counted_in_the_bucket_that_contains_it_and_the_rest_aside(
    binning: Binning, values: np.ndarray
) -> None:
    """
    An exponential binning's buckets below zero mirror those above, edges included: `-1` is in `[-10, -1]`.

    Nothing to bin is a valid empty histogram.
    """
    histogram = binning.histogram(values)
    finite = values[np.isfinite(values)]

    assert histogram.counts == _recount(histogram, values, mirrored=isinstance(binning, ExponentialBinning))
    assert (sum(histogram.counts), histogram.nonfinite) == (finite.size, values.size - finite.size)
    assert not finite.size or histogram.edges[0] <= finite.min() <= finite.max() <= histogram.edges[-1]


@given(_EXPONENTIAL, _VALUES)
def test_exponential_edges_sit_on_a_lattice_the_data_does_not_choose(
    binning: ExponentialBinning, values: np.ndarray
) -> None:
    edges = np.abs(binning.histogram(values).edges)

    steps = np.log10(edges[edges > 0]) * binning.buckets_per_decade
    assert np.allclose(steps, np.round(steps))


@given(_EXPONENTIAL, _VALUES)
def test_exponential_buckets_mirror_for_negative_values(
    binning: ExponentialBinning, values: np.ndarray
) -> None:
    positive = binning.histogram(values)
    negative = binning.histogram(-values)

    assert negative.counts == positive.counts[::-1]
    assert negative.edges == [-edge for edge in positive.edges[::-1]]


@given(_EXPONENTIAL, _VALUES)
def test_exponential_buckets_reach_no_further_than_the_values_and_leave_a_run_of_empty_ones_as_one(
    binning: ExponentialBinning, values: np.ndarray
) -> None:
    """So the payload is as small as the values' spread, however far apart they are."""
    counts = binning.histogram(values).counts

    assert (counts[0] and counts[-1]) or not any(counts)
    assert all(before or after for before, after in pairwise(counts))


def test_exponential_puts_everything_below_the_threshold_in_one_zero_bucket() -> None:
    histogram = ExponentialBinning(zero_threshold=1e-3).histogram(np.array([0.0, -1e-9, 1e-9, 5.0]))

    (zero_bucket,) = (
        i
        for i, (low, high) in enumerate(zip(histogram.edges, histogram.edges[1:], strict=False))
        if low < 0 < high
    )
    assert histogram.counts[zero_bucket] == 3


@given(_EDGES, _VALUES)
def test_explicit_buckets_are_the_given_edges_reaching_out_to_any_value_outside_them(
    edges: list[float], values: np.ndarray
) -> None:
    finite = values[np.isfinite(values)]
    low, high = (finite.min(), finite.max()) if finite.size else (edges[0], edges[-1])

    histogram = ExplicitBinning(edges=edges).histogram(values)

    assert histogram.edges == [
        *([low] if low < edges[0] else []),
        *edges,
        *([high] if high > edges[-1] else []),
    ]


@given(st.lists(st.floats(-3, 3) | st.sampled_from([math.nan, math.inf]), max_size=4))
@example(edges=[float(edge) for edge in range(2000)])  # more buckets than the cap
def test_explicit_binning_takes_finite_strictly_increasing_edges_of_one_bucket_or_more(
    edges: list[float],
) -> None:
    usable = (
        2 <= len(edges) <= 1025  # one bucket, and `DLBOARD_MAX_HISTOGRAM_BUCKETS`'s default
        and all(math.isfinite(edge) for edge in edges)
        and all(low < high for low, high in pairwise(edges))
    )

    if usable:
        assert ExplicitBinning(edges=edges).edges == edges
    else:
        with pytest.raises(ValidationError):
            ExplicitBinning(edges=edges)


def test_a_binning_that_needs_more_buckets_than_the_cap_fails_where_the_values_are_binned() -> None:
    values = 10.0 ** np.linspace(-30, 30, 5000)

    with pytest.raises(ValidationError, match="at most"):
        ExponentialBinning(buckets_per_decade=500).histogram(values)


@pytest.mark.parametrize(
    ("values", "total"),
    [
        ([0.5, 1.5, 100.0], 3),
        (np.array([0.5, 1.5, 100.0], dtype=np.float16), 3),
        (np.array([[0.5, 1.5], [100.0, 100.0]]), 4),
        (torch.tensor([0.5, 1.5, 100.0], dtype=torch.bfloat16), 3),
        (torch.tensor([0.5, 1.5, 100.0], requires_grad=True), 3),
        (torch.tensor([1, 2, 100]), 3),
    ],
)
def test_any_array_like_is_binned(values: ArrayLike, total: int) -> None:
    histogram = ExponentialBinning().histogram(values)

    assert sum(histogram.counts) == total
    assert histogram.nonfinite == 0
