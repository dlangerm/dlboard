"""
Bin values into a `Histogram` in the training process, at the call that logs them.

The cost of a histogram is paid here, in one O(n) pass over the values, so a binning that is expensive
(or a tensor that is huge) slows down only the script that chose it. What reaches the server is the
bucket edges and counts, never the values.
"""

from __future__ import annotations

import math
import typing

import numpy as np
from pydantic import BaseModel, Field

from dlboard.client._arrays import ArrayLike, to_numpy
from dlboard.models import Histogram, HistogramEdges


def _finite_values(values: ArrayLike) -> tuple[np.ndarray, int]:
    """
    `values` as one flat floating-point numpy array without its NaNs and infinities, and how many of those it had.

    Half-precision and integer dtypes are widened: float16 is too coarse to find a bucket in.
    """
    flat = to_numpy(values).reshape(-1)
    flat = flat if flat.dtype in (np.float32, np.float64) else flat.astype(np.float64)
    finite = flat[np.isfinite(flat)]
    return finite, flat.size - finite.size


def _boundary(index: np.ndarray, zero_exponent: int, buckets_per_decade: int) -> np.ndarray:
    """
    The exponential lattice's boundaries: `+10**((zero_exponent + i) / n)` for `i >= 0`, mirrored below zero.

    Every bucket boundary comes out of this one expression, so two buckets that touch share a bit-identical edge.
    """
    # `|i + 1/2| - 1/2` is `i` above zero and `-i - 1` below it, and `i + 1/2` has the sign of the side.
    exponent = zero_exponent + np.abs(index + 0.5) - 0.5
    return np.copysign(np.power(10.0, exponent / buckets_per_decade), index + 0.5)


class ExponentialBinning(BaseModel, frozen=True, extra="forbid"):
    """
    Buckets whose width grows with the magnitude of the values: a fixed number per power of ten, mirrored for negatives.

    The right default for gradient norms, losses and weights, whose interesting structure is spread over many orders
    of magnitude. The edges sit on a lattice that depends only on these two settings, never on the data, so every
    step and every run logged with them lines up bucket for bucket.
    """

    buckets_per_decade: int = Field(default=10, ge=1)
    """How many buckets cover each power of ten: more is finer, and costs more buckets for data spread over many decades."""

    zero_threshold: float = Field(default=1e-12, gt=0)
    """Values smaller in magnitude than this (rounded up to the lattice) share one bucket around zero."""

    def histogram(self, values: ArrayLike) -> Histogram:
        """Bin `values`. Raises if they spread over more than `DLBOARD_MAX_HISTOGRAM_BUCKETS` buckets."""
        finite, nonfinite = _finite_values(values)
        n = self.buckets_per_decade
        zero_exponent = math.ceil(math.log10(self.zero_threshold) * n)
        cutoff = 10.0 ** (zero_exponent / n)
        if not finite.size:
            return Histogram(edges=[-cutoff, cutoff], counts=[0], nonfinite=nonfinite)

        # Bucket 0 is the zero bucket; bucket `p > 0` is the p-th lattice step above it, and `-p` its mirror. Built
        # in place in one array the size of the values, since a tensor can be big.
        in_zero_bucket: np.ndarray = np.abs(finite) < cutoff
        bucket = np.abs(finite)
        np.maximum(bucket, cutoff, out=bucket)
        np.log10(bucket, out=bucket)
        bucket *= n
        np.floor(bucket, out=bucket)
        np.maximum(bucket, zero_exponent, out=bucket)
        bucket -= zero_exponent - 1
        np.copysign(bucket, finite, out=bucket)
        bucket[in_zero_bucket] = 0
        bucket = bucket.astype(np.int64)

        first = int(bucket.min())
        tally = np.bincount(bucket - first)
        populated = np.flatnonzero(tally)
        lows = _boundary(populated + first - 1, zero_exponent, n)
        highs = _boundary(populated + first, zero_exponent, n)

        # A run of untouched buckets between two populated ones is one zero-count bucket, so the payload
        # stays as small as the data's spread, not its range.
        gaps = np.flatnonzero(lows[1:] != highs[:-1]) + 1
        edges = np.insert(np.concatenate(([lows[0]], highs)), gaps + 1, lows[gaps])
        counts = np.insert(tally[populated], gaps, 0)
        return Histogram(edges=edges.tolist(), counts=counts.tolist(), nonfinite=nonfinite)


class ExplicitBinning(BaseModel, frozen=True, extra="forbid"):
    """
    Buckets at exactly the edges you give.

    Values outside them are not dropped: an extra bucket reaching the smallest or largest value seen holds them.
    """

    edges: HistogramEdges
    """The bucket boundaries, strictly increasing. A value equal to an edge falls in the bucket starting there."""

    def histogram(self, values: ArrayLike) -> Histogram:
        """Bin `values` into these edges."""
        finite, nonfinite = _finite_values(values)
        edges = list(self.edges)
        if finite.size:
            if (lowest := float(finite.min())) < edges[0]:
                edges.insert(0, lowest)
            if (highest := float(finite.max())) > edges[-1]:
                edges.append(highest)
        counts, _ = np.histogram(finite, bins=edges)
        return Histogram(edges=edges, counts=counts.tolist(), nonfinite=nonfinite)


Binning: typing.TypeAlias = ExponentialBinning | ExplicitBinning
"""How `DLBoardLogger.log_histograms` turns values into buckets."""

DEFAULT_BINNING: typing.Final = ExponentialBinning()
