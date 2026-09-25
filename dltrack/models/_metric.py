"""The data model for a metric value logged at a step for a particular experiment."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, NamedTuple, cast

import pandas as pd
from pydantic import AwareDatetime, BaseModel, field_validator

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


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


class MetricKeySummary(NamedTuple):
    """One metric key logged in an experiment, summarized without fetching its values."""

    key: str
    max_steps_per_run: int
    """The most distinct steps any one run logged this key at -- 1 means "a single value per run"."""


class MetricRow(NamedTuple):
    """One stored metric value -- the exact row shape `DataStore.fetch_metrics` reads, in insertion order."""

    run_id: int
    step: int
    key: str
    value: float | None
    timestamp_utc: str
    """ISO-8601, as stored."""


_KEY, _VALUE = MetricRow._fields[2], MetricRow._fields[3]
_INDEX = [MetricColumn.RUN_ID, MetricColumn.STEP]


@dataclass(frozen=True)
class MetricFrame:
    """
    Metrics in wide form: one row per `(run_id, step)`, one `float64` column per metric key.

    Only ever built by `from_rows`, which is what makes these hold: the `MetricColumn`s are always
    present with fixed dtypes, `(run_id, step)` is unique and sorted, and `keys` is exactly the set
    of every other column. Metric *names* are data, so they can't be typed statically -- read them
    through `keys`/`values`, and hand `to_frame()` to the (untyped) chart layer.
    """

    keys: frozenset[str]
    _frame: pd.DataFrame = field(repr=False)

    @classmethod
    def from_rows(cls, rows: Iterable[MetricRow]) -> MetricFrame:
        """
        Pivot stored rows, in the order they were written, into wide form.

        A key logged more than once at the same `(run_id, step)` keeps the value written *last*; a
        row's timestamp is the earliest of those logged at its `(run_id, step)`.
        """
        long = pd.DataFrame.from_records(list(rows), columns=MetricRow._fields)
        long = long.drop_duplicates(subset=[*_INDEX, _KEY], keep="last")
        long[MetricColumn.TIMESTAMP_UTC] = pd.to_datetime(
            long[MetricColumn.TIMESTAMP_UTC], utc=True, format="ISO8601"
        )
        keys = frozenset[str](long[_KEY])
        if reserved := sorted(keys & set(MetricColumn)):
            msg = f"Stored metrics use reserved column names {reserved}"
            raise ValueError(msg)
        values = long.pivot(index=_INDEX, columns=_KEY, values=_VALUE)
        timestamps = long.groupby(_INDEX)[MetricColumn.TIMESTAMP_UTC].min()
        frame = (
            values.join(timestamps)
            .reset_index()
            .astype(
                {MetricColumn.RUN_ID: "int64", MetricColumn.STEP: "int64"} | dict.fromkeys(keys, "float64")
            )
        )
        frame.columns.name = None
        return cls(keys, frame)

    @property
    def run_id(self) -> pd.Series[int]:
        return self._frame[MetricColumn.RUN_ID]

    @property
    def step(self) -> pd.Series[int]:
        return self._frame[MetricColumn.STEP]

    @property
    def timestamp_utc(self) -> pd.Series[pd.Timestamp]:
        return self._frame[MetricColumn.TIMESTAMP_UTC]

    def values(self, key: str) -> pd.Series[float]:
        if key not in self.keys:
            raise KeyError(key)
        return self._frame[key]

    def latest_per_run(self) -> dict[int, dict[str, float]]:
        """Each run's most recently logged (highest-step) value of every key it has logged at all."""
        latest = self._frame.groupby(MetricColumn.RUN_ID)[sorted(self.keys)].last()
        return {
            cast("int", run_id): {cast("str", key): value for key, value in row.items() if pd.notna(value)}
            for run_id, row in latest.to_dict(orient="index").items()
        }

    def to_frame(self) -> pd.DataFrame:
        """A copy of the underlying dataframe, for the (untyped) chart layer."""
        return self._frame.copy()
