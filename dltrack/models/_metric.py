"""The data model for a metric value logged at a step for a particular experiment."""

from collections.abc import Iterable
from typing import Iterator

from pydantic import BaseModel


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
    step: int | None
    """Step the metric was taken at."""


class LoggedMetrics(BaseModel, frozen=True, extra="forbid"):
    """A mapping of keys to values for a step."""

    metrics: dict[str, float | None] = {}
    """The k/v pairs of metric values."""
    step: int | None = None
    """The step these metrics were logged at."""
    experiment_id: int
    """The experiment to associate with the metrics."""
    run_id: int
    """The run to associate with the metrics."""

    def to_underlying(self) -> Iterator[UnderlyingMetricTableEntry]:
        """Convert a bulk metrics set into a table entry."""
        for key, value in self.metrics.items():
            yield UnderlyingMetricTableEntry.model_construct(
                key=key,
                value=value,
                experiment_id=self.experiment_id,
                run_id=self.run_id,
                step=self.step,
            )

    @classmethod
    def from_underlying(cls, entries: Iterable[UnderlyingMetricTableEntry]) -> "Iterator[LoggedMetrics]":
        cur_raw_metrics: dict[str, float | None] = {}
        cur_step = None
        cur_run_id = None
        experiment_id = None
        for entry in entries:
            experiment_id = entry.experiment_id

            if cur_step is not None and (entry.step != cur_step or entry.run_id != cur_run_id):
                cur_run_id = cur_run_id or entry.run_id
                assert experiment_id is not None
                yield LoggedMetrics.model_construct(
                    metrics=cur_raw_metrics,
                    step=cur_step,
                    experiment_id=experiment_id,
                    run_id=cur_run_id,
                )
                cur_raw_metrics = {}

            if entry.key in cur_raw_metrics:
                continue

            cur_raw_metrics[entry.key] = entry.value
            cur_step = entry.step
            cur_run_id = entry.run_id

        if experiment_id is not None and cur_run_id is not None:
            yield LoggedMetrics.model_construct(
                metrics=cur_raw_metrics,
                step=cur_step,
                experiment_id=experiment_id,
                run_id=cur_run_id,
            )
