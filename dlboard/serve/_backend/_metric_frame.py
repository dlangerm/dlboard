"""
`MetricFrame`: a `DataStore`'s own wide-form view of logged metrics, and what reads them.

Split out of `dlboard.models._metric` -- pandas is a server-only dependency, and a client never
touches any of these three (it only ever constructs a `LoggedMetrics` to send, never reads one back
in wide form).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple, cast

import pandas as pd

from dlboard.models._metric import MetricColumn

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


class MetricKeySummary(NamedTuple):
    """One metric key logged in an experiment, summarized without fetching its values."""

    key: str
    max_steps_per_run: int
    """
    The longest span, in steps, that one of the newest runs logged this key over (its last step less its first, plus
    one), or 0 if none of them logged it. An upper bound on the most steps a run logged, but exactly 1 when no such run
    logged more than one step -- one value per run, a natural fit for a bar chart.
    """


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
    def from_rows(cls, rows: Iterable[Sequence[object]]) -> MetricFrame:
        """
        Pivot stored rows (a `MetricRow` each, as a tuple of its fields), in the order they were written, into wide form.

        A timestamp is an ISO-8601 string or an aware datetime.

        A key logged more than once at the same `(run_id, step)` keeps the value written *last*; a
        row's timestamp is the earliest of those logged at its `(run_id, step)`.
        """
        long = pd.DataFrame(list(rows), columns=list(MetricRow._fields))
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

    @classmethod
    def merged(cls, frames: Iterable[MetricFrame]) -> MetricFrame:
        """
        One frame with every row and key of `frames`: a row is wherever any of them has one.

        For a panel whose charts need different `MetricReduction`s of different keys, which is why no key may be
        in two frames: whichever came first would silently win.
        """
        frames = list(frames)
        if len(frames) <= 1:
            return frames[0] if frames else cls.from_rows([])
        counted = Counter(key for frame in frames for key in frame.keys)
        if shared := sorted(key for key, count in counted.items() if count > 1):
            msg = f"Metrics fetched more than once cannot be merged: {shared}"
            raise ValueError(msg)
        keys = frozenset(counted)
        wide = (
            pd.concat([frame._frame for frame in frames], ignore_index=True)  # noqa: SLF001
            .groupby(_INDEX, as_index=False)
            .agg(dict.fromkeys(keys, "first") | {MetricColumn.TIMESTAMP_UTC: "min"})
        )
        return cls(keys, wide)

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
        """
        Each run's latest value of every key it has logged, for a frame of `MetricReduction.LATEST` rows.

        Each key has one row per run there, so `last` is that value, and a `NaN` (no value) is left out.
        """
        latest = self._frame.groupby(MetricColumn.RUN_ID)[sorted(self.keys)].last()
        return {
            cast("int", run_id): {cast("str", key): value for key, value in row.items() if pd.notna(value)}
            for run_id, row in latest.to_dict(orient="index").items()
        }

    def to_frame(self) -> pd.DataFrame:
        """A copy of the underlying dataframe, for the (untyped) chart layer."""
        return self._frame.copy()
