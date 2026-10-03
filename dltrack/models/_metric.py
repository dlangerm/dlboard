"""The data model for a metric value logged at a step for a particular experiment."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import AwareDatetime, BaseModel, field_validator

if TYPE_CHECKING:
    from collections.abc import Iterator


class MetricColumn(StrEnum):
    """The fixed, non-metric columns of every `MetricFrame` -- so no metric may be logged under these names."""

    RUN_ID = "run_id"
    STEP = "step"
    TIMESTAMP_UTC = "timestamp_utc"


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


class LoggedMetrics(BaseModel, frozen=True, extra="forbid"):
    """A mapping of keys to values for a step."""

    metrics: dict[str, float | None] = {}
    """The k/v pairs of metric values."""
    step: int
    """The step these metrics were logged at."""
    experiment_id: int
    """The experiment to associate with the metrics."""
    run_id: int
    """The run to associate with the metrics."""
    timestamp_utc: AwareDatetime
    """Timestamp of the metric."""

    @field_validator("metrics")
    @classmethod
    def _reject_reserved_keys(cls, metrics: dict[str, float | None]) -> dict[str, float | None]:
        if reserved := sorted(metrics.keys() & set(MetricColumn)):
            msg = f"{reserved} are reserved column names and can't be logged as metrics"
            raise ValueError(msg)
        return metrics

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
