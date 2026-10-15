"""Arithmetic on binned histograms for the histogram chart (numpy only: no Dash, no chart types)."""

from __future__ import annotations

import math
import typing

import numpy as np

if typing.TYPE_CHECKING:
    from collections.abc import Sequence

    from dlboard.models import Histogram


def common_edges(histograms: Sequence[Histogram], max_rows: int) -> np.ndarray:
    """
    The union of every histogram's edges, so they can all be drawn on the same rows.

    Binned on the same lattice (the default), the union adds nothing, and each row is a real bucket.
    More than `max_rows` rows are merged by keeping every n-th edge.
    """
    edges = np.unique(np.concatenate([h.edges for h in histograms]))
    rows = len(edges) - 1
    if rows <= max_rows:
        return edges
    return np.append(edges[: -1 : math.ceil(rows / max_rows)], edges[-1])


def regrid(histogram: Histogram, edges: np.ndarray) -> np.ndarray:
    """
    `histogram`'s counts on the rows between `edges`, each source bucket's count spread evenly across its width.

    Exact where the edges line up (the rows of one lattice), and a proportional split where they do not.
    Counts outside `histogram`'s own edges are zero, and the total is conserved.
    """
    cumulative: np.ndarray = np.concatenate(([0.0], np.cumsum(histogram.counts)))
    interpolated: np.ndarray = np.interp(edges, histogram.edges, cumulative)
    return np.diff(interpolated)


def spread(histogram: Histogram, quantiles: Sequence[float]) -> np.ndarray:
    """
    Where the values of `histogram` lie: `[min, *quantiles, max]`, all NaN if it holds none.

    Read off the bucket edges, so it is only as precise as the buckets are -- within a bucket, values are taken
    to be spread evenly. The extremes are the outer edges of the first and last non-empty bucket. Each quantile is
    strictly between 0 and 1.
    """
    counts = np.asarray(histogram.counts, dtype=float)
    total = counts.sum()
    if total == 0:
        return np.full(len(quantiles) + 2, np.nan)
    edges = np.asarray(histogram.edges)
    cumulative: np.ndarray = np.concatenate(([0.0], np.cumsum(counts)))
    targets: np.ndarray = np.asarray(quantiles) * total
    # The last bucket whose cumulative count is at most the target: strictly inside the distribution,
    # so it is never empty, and the division below never is by zero.
    bucket: np.ndarray = np.searchsorted(cumulative, targets, side="right") - 1
    inner: np.ndarray = edges[bucket] + (targets - cumulative[bucket]) / counts[bucket] * (
        edges[bucket + 1] - edges[bucket]
    )
    occupied: np.ndarray = np.flatnonzero(counts)
    first, last = int(occupied[0]), int(occupied[-1])
    return np.array([edges[first], *inner, edges[last + 1]])
