"""The data model for a metric value logged at a step for a particular experiment."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, TypeVar

from pydantic import AwareDatetime, BaseModel, Field, field_validator

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

    @property
    def reads_every_step(self) -> bool:
        """Whether answering reads every step of a series: all but the latest and the first, which are probes."""
        return self not in (MetricReduction.LATEST, MetricReduction.FIRST)


class MetricReductions(BaseModel, frozen=True, extra="forbid"):
    """
    How a chart reads each metric it draws from: `default` for every metric, unless `by_column` names it.

    A chart that reads one metric's latest value and another's whole series says so per column. A chart that reads
    every metric the same way need only set `default`.
    """

    default: MetricReduction = MetricReduction.HISTORY
    by_column: dict[str, MetricReduction] = Field(default_factory=dict)

    def of(self, column: str) -> MetricReduction:
        """How the metric called `column` is read."""
        return self.by_column.get(column, self.default)

    def __hash__(self) -> int:
        """As a frozen model's, but its columns' reductions too, which a dict cannot be: so a `DataRequest` is a key."""
        return hash((self.default, frozenset(self.by_column.items())))


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


class SeriesBand(NamedTuple):
    """
    The spread of a group of keys' values over one cell of steps of one run.

    `count` is how many of the group's keys logged a number in the cell (each key's newest write in it). The other
    statistics are of those numbers: the nearest-rank minimum, 25th percentile, median, 75th percentile and maximum,
    and their mean, with the key that logged the minimum and the one that logged the maximum. `step` is the last
    step of the cell. `group` is the label the keys were grouped under (`""` for keys grouped as one).
    """

    run_id: int
    group: str
    step: int
    count: int
    minimum: float
    p25: float
    median: float
    p75: float
    maximum: float
    mean: float
    minimum_key: str
    maximum_key: str
