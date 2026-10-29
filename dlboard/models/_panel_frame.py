"""
The columns of a panel's dataframe: what a chart type's `render` is handed, and what the page fetches into it.

One wide frame per panel -- a row per (run, step), the metrics as they were logged, and the hyperparameters
and artifacts merged in under prefixed names so none can collide with a metric. This is the contract between
the experiment page's fetch and every chart type, so both sides name its columns through here rather than
repeating the strings.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import typing

from dlboard.models._key_patterns import KeyPatterns
from dlboard.models._metric import MetricColumn
from dlboard.models._view import ColumnKind

if typing.TYPE_CHECKING:
    from collections.abc import Collection, Iterable

RUN_NAME_COLUMN: typing.Final = "run_name"
"""
The column a run's display name is carried under, when a chart's dataframe has one.

Not a real metric/hparam/artifact column -- never fetched from the store directly, and not part
of `MetricColumn`'s "fixed columns of a `MetricFrame`" -- it's merged onto a panel's fetched
dataframe afterward (see `PanelData.for_chart`), so a chart can label a run by name instead of
its bare id without fetching run metadata itself. A chart that needs to *exclude* non-metric columns
(e.g. `table_chart.py`'s "runs" mode) can name it precisely rather than guessing at string prefixes.
"""

EXPERIMENT_COLUMN: typing.Final = "experiment_id"
"""
The experiment a run is of, beside its name: which a chart that goes back to the server for more of its data (an image
chart's window of steps) needs to ask for it, since a page can draw runs of several experiments (a comparison).
"""

NO_DATA_COLUMNS: typing.Final = frozenset({*MetricColumn, RUN_NAME_COLUMN, EXPERIMENT_COLUMN})
"""Columns that say where a row is, not what a chart shows: present on every row whatever was logged."""

IDENTITY_COLUMNS: typing.Final = frozenset({MetricColumn.RUN_ID, RUN_NAME_COLUMN, EXPERIMENT_COLUMN})
"""Which run a row belongs to, and what to call it: part of what every chart shows, though no chart asks for them."""

HPARAM_COLUMN_PREFIX: typing.Final = "hparam__"
"""Hyperparameter columns are merged into the same wide dataframe as metrics/artifacts, prefixed
so a hparam key can never collide with a same-named metric or artifact column."""

ARTIFACT_COLUMN_PREFIX: typing.Final = "artifact__"
"""Artifact columns are prefixed for the same reason -- see `artifact_column`/`artifact_tags_column`."""

HISTOGRAM_COLUMN_PREFIX: typing.Final = "histogram__"
"""Histogram columns are prefixed for the same reason; each cell holds a `Histogram`, or NaN where the key was not logged."""

SERIES_BAND_COLUMN_PREFIX: typing.Final = "series_band__"
"""Series-band columns are prefixed for the same reason; each cell holds a `SeriesBand`, or NaN where the grid has no step."""


BAND_PROBE_BUDGET: typing.Final = 100_000
"""
Most index probes one band read makes: one for each metric, run and cell. About a second on SQLite, and a live page
repeats it every poll while a run trains, so a band over thousands of metrics gets fewer cells rather than a slower read.
"""

MIN_BAND_CELLS: typing.Final = 4
"""The fewest cells a band has, however many metrics it spreads over."""


@dataclasses.dataclass(frozen=True)
class SeriesBandRequest:
    """
    What a chart asks for, as the spread of the values of the metrics its `patterns` match, on a grid of `grid` steps.

    The metrics can be grouped by part of their names, so a family like `accuracy/{row},{col}/{class}` is read once
    for a band per class (`patterns={"accuracy/#,#/*"}`, `group_by=(2,)`), or per grid cell (`group_by=(0, 1)`).
    """

    patterns: frozenset[str]
    """The metric names and patterns (see `KeyPatterns`) whose values are spread."""
    grid: int
    """At most this many cells of steps, see `_grid_cells` in the store, and fewer over many metrics (`cells_for`)."""
    runs: int
    """How many of the panel's newest runs are spread, each costing a read of every metric the patterns match."""
    group_by: tuple[int, ...] = ()
    """
    Which wildcards of a pattern (counted from 0, see `KeyPatterns.capture`) name a metric's group; none puts every
    metric in one. A metric whose pattern has fewer wildcards is grouped by the ones it has.
    """
    max_groups: int = 12
    """The most groups read: the first this many by label. Each is a band per run and cell for a chart to draw."""

    @property
    def column(self) -> str:
        """The column its bands are carried under: a digest of what it asks for, so charts that ask alike share it."""
        asked = json.dumps([sorted(self.patterns), self.grid, self.runs, self.group_by, self.max_groups])
        digest = hashlib.sha1(asked.encode(), usedforsecurity=False).hexdigest()[:12]
        return f"{SERIES_BAND_COLUMN_PREFIX}{digest}"

    def cells_for(self, metrics: int, runs: int) -> int:
        """
        How many cells the grid has when `metrics` are read for `runs`: `grid`, or fewer to stay in `BAND_PROBE_BUDGET`.

        The read costs a probe for each metric, run and cell, so it is the cells that give way as the metrics grow.
        """
        return max(MIN_BAND_CELLS, min(self.grid, BAND_PROBE_BUDGET // max(1, metrics * runs)))

    def groups(self, known_keys: Collection[str]) -> dict[str, str]:
        """
        The metrics among `known_keys` that `patterns` match, each with the label of its group.

        A label is the text of the wildcards `group_by` names, joined by `", "` (`""` without `group_by`), and only the
        first `max_groups` labels, in order, are kept.
        """
        asked = KeyPatterns(tuple(self.patterns))
        labelled: dict[str, str] = {}
        for key in asked.select(known_keys):
            captured = asked.capture(key) or ()
            labelled[key] = ", ".join(captured[at] for at in self.group_by if at < len(captured))
        kept = set(sorted(set(labelled.values()))[: self.max_groups])
        return {key: label for key, label in labelled.items() if label in kept}


_TAGS_SUFFIX: typing.Final = "__tags"
_FNAME_SUFFIX: typing.Final = "__fname"
_LOGGED_SUFFIX: typing.Final = "__logged"


def histogram_column(key: str) -> str:
    """The panel-dataframe column holding each step's `Histogram` for histogram `key`."""
    return f"{HISTOGRAM_COLUMN_PREFIX}{key}"


def artifact_column(key: str) -> str:
    """The panel-dataframe column holding each step's download URL for artifact `key`."""
    return f"{ARTIFACT_COLUMN_PREFIX}{key}"


def artifact_tags_column(key: str) -> str:
    """The panel-dataframe column holding each step's tags dict for artifact `key`."""
    return f"{artifact_column(key)}{_TAGS_SUFFIX}"


def artifact_fname_column(key: str) -> str:
    """The panel-dataframe column holding each step's file name for artifact `key` (its URL carries none)."""
    return f"{artifact_column(key)}{_FNAME_SUFFIX}"


def artifact_logged_column(key: str) -> str:
    """
    The panel-dataframe column marking each step `key` logged an artifact at, `True`, for a chart reading a window of them.

    Its URL, tags and file name are read only for the steps of the window (`ArtifactWindow`): this says where the rest are.
    """
    return f"{artifact_column(key)}{_LOGGED_SUFFIX}"


def artifact_keys_in(columns: Iterable[str], name: str, *, prefix: bool) -> list[str]:
    """
    The artifact keys among a panel dataframe's `columns` that `name` asks for, in column order.

    `name` is a key, or with `prefix` every key that starts with it. A key has one column of URLs and
    one each for its tags and file names, which are not keys of their own.
    """
    keys = [
        column.removeprefix(ARTIFACT_COLUMN_PREFIX)
        for column in columns
        if column.startswith(ARTIFACT_COLUMN_PREFIX)
        and not column.endswith((_TAGS_SUFFIX, _FNAME_SUFFIX, _LOGGED_SUFFIX))
    ]
    return [key for key in keys if (key.startswith(name) if prefix else key == name)]


def hparam_column(key: str) -> str:
    """The panel-dataframe column holding each run's value of hyperparameter `key`."""
    return f"{HPARAM_COLUMN_PREFIX}{key}"


def column_kind(column: str) -> ColumnKind:
    """
    What a panel dataframe's `column` holds, from its prefix: an unprefixed one is a metric (or a fixed column).

    The inverse of the `*_column` helpers above, so a caller that has to treat each kind differently
    `match`es on this instead of testing prefixes itself.
    """
    if column.startswith(HPARAM_COLUMN_PREFIX):
        return ColumnKind.HPARAM
    if column.startswith(ARTIFACT_COLUMN_PREFIX):
        return ColumnKind.ARTIFACT
    if column.startswith(HISTOGRAM_COLUMN_PREFIX):
        return ColumnKind.HISTOGRAM
    if column.startswith(SERIES_BAND_COLUMN_PREFIX):
        return ColumnKind.SERIES_BAND
    return ColumnKind.METRIC
