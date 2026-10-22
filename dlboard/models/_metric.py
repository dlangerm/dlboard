"""The data model for a metric value logged at a step for a particular experiment."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from pydantic import AwareDatetime, BaseModel, field_validator

from dlboard._compat import StrEnum
from dlboard.models._histogram import Histogram, UnderlyingHistogramTableEntry

if TYPE_CHECKING:
    from collections.abc import Iterator

# Pre-3.12 `TypeVar` style: the client's floor is 3.10.
_Logged = TypeVar("_Logged")


class MetricColumn(StrEnum):
    """The fixed, non-metric columns of every `MetricFrame` -- so no metric may be logged under these names."""

    RUN_ID = "run_id"
    STEP = "step"
    TIMESTAMP_UTC = "timestamp_utc"


class MetricReduction(StrEnum):
    """
    How much of a metric's series a reader needs: what a store has to read to answer, and so what it costs.

    Anything but `HISTORY` is answered with a row per run and key (at the series' latest step for the
    ones that have no step of their own), however many steps were logged -- see `DataStore.fetch_metrics`.
    """

    HISTORY = "history"
    """Every step. A chart that draws the series itself, or one that has not said otherwise."""
    LATEST = "latest"
    FIRST = "first"
    MIN = "min"
    MAX = "max"
    MEAN = "mean"


class UnderlyingMetricTableEntry(BaseModel, frozen=True, extra="forbid"):
    """The underlying table schema of individual metrics."""

    id: int
    """Row ID."""
    key: str
    """Name of the metric."""
    value: float | None
    """Value of the metric."""
    experiment_id: int
    """Experiment Id for the metric."""
    run_id: int
    """Run Id for the metric."""
    step: int
    """Step the metric was taken at."""
    timestamp_utc: AwareDatetime
    """Time recorded from the client."""


class LoggedMetrics(BaseModel, frozen=True, extra="ignore"):
    """A mapping of keys to values for a step. `extra="ignore"`: this crosses the wire -- see `dlboard._wire`."""

    metrics: dict[str, float | None] = {}
    """The k/v pairs of metric values."""
    histograms: dict[str, Histogram] = {}
    """The distributions logged at this step, by key. They ride the metrics batch; a server that predates them ignores the field."""
    step: int
    """The step these metrics were logged at."""
    experiment_id: int
    """The experiment to associate with the metrics."""
    run_id: int
    """The run to associate with the metrics."""
    timestamp_utc: AwareDatetime
    """Timestamp of the metric."""

    @field_validator("metrics", "histograms")
    @classmethod
    def _reject_reserved_keys(cls, logged: dict[str, _Logged]) -> dict[str, _Logged]:
        if reserved := sorted(logged.keys() & set(MetricColumn)):
            msg = f"{reserved} are reserved column names and can't be logged as metrics"
            raise ValueError(msg)
        return logged

    def to_underlying(self) -> Iterator[UnderlyingMetricTableEntry]:
        """Convert a bulk metrics set into a table entry."""
        for key, value in self.metrics.items():
            yield UnderlyingMetricTableEntry.model_construct(
                key=key,
                value=value,
                experiment_id=self.experiment_id,
                run_id=self.run_id,
                step=self.step,
                timestamp_utc=self.timestamp_utc,
            )

    def to_underlying_histograms(self) -> Iterator[UnderlyingHistogramTableEntry]:
        """Convert this step's histograms into table entries."""
        for key, histogram in self.histograms.items():
            yield UnderlyingHistogramTableEntry.model_construct(
                key=key,
                experiment_id=self.experiment_id,
                run_id=self.run_id,
                step=self.step,
                timestamp_utc=self.timestamp_utc,
                edges=histogram.edges,
                counts=histogram.counts,
                nonfinite=histogram.nonfinite,
            )
