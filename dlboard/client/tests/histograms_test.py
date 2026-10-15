"""Tests for binning values into histograms on the client."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest
import torch
from pydantic import ValidationError

from dlboard.client.histograms import ExplicitBinning, ExponentialBinning

if TYPE_CHECKING:
    from dlboard.client._arrays import ArrayLike
    from dlboard.client.histograms import Binning
    from dlboard.models import Histogram

_RNG = np.random.default_rng(0)
_SPREAD: np.ndarray = _RNG.standard_normal(5000) * 10.0 ** _RNG.integers(-6, 6, 5000)
"""Positive and negative values over twelve orders of magnitude."""

_BINNINGS = [
    ExponentialBinning(),
    ExplicitBinning(edges=[0.0, 1.0, 2.0]),
]


def _recount(histogram: Histogram, values: np.ndarray) -> list[int]:
    """Where `values` fall among `histogram`'s edges, counted independently of how it was binned."""
    finite: np.ndarray = values[np.isfinite(values)]
    index: np.ndarray = np.clip(
        np.searchsorted(histogram.edges, finite, side="right") - 1, 0, len(histogram.counts) - 1
    )
    return np.bincount(index, minlength=len(histogram.counts)).tolist()


@pytest.mark.parametrize(
    "binning",
    [
        ExponentialBinning(),
        ExponentialBinning(buckets_per_decade=3, zero_threshold=1e-4),
        ExplicitBinning(edges=[-1.0, 0.0, 0.5, 100.0]),
    ],
)
def test_every_value_is_counted_in_the_bucket_that_contains_it(binning: Binning) -> None:
    histogram = binning.histogram(_SPREAD)

    assert histogram.counts == _recount(histogram, _SPREAD)
    assert sum(histogram.counts) == len(_SPREAD)


@pytest.mark.parametrize("values", [np.array([1.5, 2.5]), _SPREAD])
def test_exponential_edges_sit_on_a_lattice_the_data_does_not_choose(values: np.ndarray) -> None:
    edges = np.abs(ExponentialBinning().histogram(values).edges)

    steps = np.log10(edges[edges > 0]) * 10
    assert np.allclose(steps, np.round(steps))


def test_exponential_buckets_mirror_for_negative_values() -> None:
    values = np.array([0.003, 0.2, 0.2, 40.0])
    positive = ExponentialBinning().histogram(values)
    negative = ExponentialBinning().histogram(-values)

    assert negative.counts == positive.counts[::-1]
    assert negative.edges == [-edge for edge in positive.edges[::-1]]


def test_exponential_leaves_a_run_of_empty_buckets_as_one() -> None:
    histogram = ExponentialBinning().histogram(np.array([1e-3, 1e3]))

    assert histogram.counts[0] == histogram.counts[-1] == 1
    assert histogram.counts[1:-1] == [0]


def test_exponential_puts_everything_below_the_threshold_in_one_zero_bucket() -> None:
    histogram = ExponentialBinning(zero_threshold=1e-3).histogram(np.array([0.0, -1e-9, 1e-9, 5.0]))

    (zero_bucket,) = (
        i
        for i, (low, high) in enumerate(zip(histogram.edges, histogram.edges[1:], strict=False))
        if low < 0 < high
    )
    assert histogram.counts[zero_bucket] == 3


@pytest.mark.parametrize("binning", _BINNINGS)
def test_nonfinite_values_are_counted_aside_not_binned(binning: Binning) -> None:
    histogram = binning.histogram(np.array([0.5, 1.5, np.nan, np.inf, -np.inf]))

    assert histogram.nonfinite == 3
    assert sum(histogram.counts) == 2


@pytest.mark.parametrize("binning", _BINNINGS)
@pytest.mark.parametrize("values", [[], [np.nan]])
def test_nothing_to_bin_is_a_valid_empty_histogram(binning: Binning, values: list[float]) -> None:
    histogram = binning.histogram(np.array(values))

    assert sum(histogram.counts) == 0
    assert histogram.nonfinite == len(values)


def test_explicit_buckets_include_values_outside_them() -> None:
    histogram = ExplicitBinning(edges=[0.0, 1.0, 2.0]).histogram(np.array([-5.0, 0.0, 1.0, 2.0, 9.0]))

    assert histogram.edges == [-5.0, 0.0, 1.0, 2.0, 9.0]
    # An edge belongs to the bucket that starts there, and the last bucket keeps its upper edge.
    assert histogram.counts == [1, 1, 1, 2]


def test_explicit_buckets_with_nothing_outside_them_are_exactly_the_given_edges() -> None:
    histogram = ExplicitBinning(edges=[0.0, 1.0, 2.0]).histogram(np.array([0.0, 0.5, 2.0]))

    assert histogram.edges == [0.0, 1.0, 2.0]
    assert histogram.counts == [2, 1]


@pytest.mark.parametrize("edges", [[1.0], [1.0, 1.0], [2.0, 1.0], [0.0, float("nan")], list(range(2000))])
def test_explicit_binning_rejects_unusable_edges(edges: list[float]) -> None:
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
