"""
A distribution of values logged at a step, already binned by the client.

Binning happens in the training process, at the call that logs it (see `dlboard.client.histograms`), so a
badly configured binning slows down only the script that chose it. The server and every chart see just
this: bucket edges and counts, never the raw values, and never more than `DLBOARD_MAX_HISTOGRAM_BUCKETS` buckets.
"""

from __future__ import annotations

import dataclasses
import functools
import itertools
import typing

from pydantic import AfterValidator, AwareDatetime, BaseModel, Field, PositiveInt, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class HistogramLimits(BaseSettings):
    """What bounds the work and the storage of every histogram, from `DLBOARD_*` env vars."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    max_histogram_buckets: PositiveInt = 1024
    """
    The most buckets one histogram may have.

    Set it on the server to change what it accepts. A client checks its own histograms against the same setting
    in its own environment, so one that bins finer needs it raised there too.
    """


@functools.cache
def max_histogram_buckets() -> int:
    """`HistogramLimits.max_histogram_buckets`, read from the environment once per process."""
    return HistogramLimits().max_histogram_buckets


_FiniteFloat = typing.Annotated[float, Field(allow_inf_nan=False)]
_Count = typing.Annotated[int, Field(ge=0)]


def _within_bucket_limit(edges: list[float]) -> list[float]:
    if len(edges) > (limit := max_histogram_buckets()) + 1:
        msg = f"at most {limit} buckets are allowed (`DLBOARD_MAX_HISTOGRAM_BUCKETS`), got {len(edges) - 1}"
        raise ValueError(msg)
    return edges


def _strictly_increasing(edges: list[float]) -> list[float]:
    if not all(low < high for low, high in itertools.pairwise(edges)):
        msg = "edges must be strictly increasing"
        raise ValueError(msg)
    return edges


HistogramEdges = typing.Annotated[
    list[_FiniteFloat],
    Field(min_length=2),
    AfterValidator(_within_bucket_limit),
    AfterValidator(_strictly_increasing),
]
"""Bucket boundaries: finite, strictly increasing, and no more than `DLBOARD_MAX_HISTOGRAM_BUCKETS` buckets' worth."""


class Histogram(BaseModel, frozen=True, extra="ignore"):
    """
    One binned distribution: bucket `i` holds `counts[i]` values in `[edges[i], edges[i + 1])`.

    The last bucket also holds its upper edge. `extra="ignore"`: this crosses the wire -- see `dlboard._wire`.
    """

    edges: HistogramEdges
    """The bucket boundaries: one more than there are buckets."""

    counts: list[_Count] = Field(min_length=1)
    """How many values fell in each bucket."""

    nonfinite: _Count = 0
    """How many NaN or infinite values were left out of `counts`."""

    @model_validator(mode="after")
    def _check_shape(self) -> Histogram:
        if len(self.edges) != len(self.counts) + 1:
            msg = f"{len(self.counts)} counts need {len(self.counts) + 1} edges, got {len(self.edges)}"
            raise ValueError(msg)
        return self


@dataclasses.dataclass(frozen=True)
class PackedFloats:
    """Marks a `list[float]` field a table stores as a packed array of 64-bit floats, not as JSON text."""


@dataclasses.dataclass(frozen=True)
class PackedCounts:
    """Marks a `list[int]` field of counts a table stores as a packed array of the narrowest unsigned integers that fit."""


class UnderlyingHistogramTableEntry(BaseModel, frozen=True, extra="forbid"):
    """The underlying table schema of individual histograms: a `Histogram` logged under `key` at `step`."""

    id: int
    """Row ID."""
    key: str
    """Name of the histogram."""
    experiment_id: int
    """Experiment Id for the histogram."""
    run_id: int
    """Run Id for the histogram."""
    step: int
    """Step the histogram was taken at."""
    timestamp_utc: AwareDatetime
    """Time recorded from the client."""
    edges: typing.Annotated[list[float], PackedFloats()]
    """See `Histogram.edges`."""
    counts: typing.Annotated[list[int], PackedCounts()]
    """See `Histogram.counts`."""
    nonfinite: int
    """See `Histogram.nonfinite`."""

    @property
    def histogram(self) -> Histogram:
        """The distribution this row stores."""
        return Histogram.model_construct(edges=self.edges, counts=self.counts, nonfinite=self.nonfinite)
