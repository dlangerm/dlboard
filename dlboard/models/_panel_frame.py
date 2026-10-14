"""
The columns of a panel's dataframe: what a chart type's `render` is handed, and what the page fetches into it.

One wide frame per panel -- a row per (run, step), the metrics as they were logged, and the hyperparameters
and artifacts merged in under prefixed names so none can collide with a metric. This is the contract between
the experiment page's fetch and every chart type, so both sides name its columns through here rather than
repeating the strings.
"""

from __future__ import annotations

import typing

from dlboard.models._metric import MetricColumn

if typing.TYPE_CHECKING:
    from collections.abc import Iterable

RUN_NAME_COLUMN: typing.Final = "run_name"
"""
The column a run's display name is carried under, when a chart's dataframe has one.

Not a real metric/hparam/artifact column -- never fetched from the store directly, and not part
of `MetricColumn`'s "fixed columns of a `MetricFrame`" -- it's merged onto a panel's fetched
dataframe afterward (see `fetch_panel_dataframe`), so a chart can label a run by name instead of
its bare id without fetching run metadata itself. A chart that needs to *exclude* non-metric columns
(e.g. `table_chart.py`'s "runs" mode) can name it precisely rather than guessing at string prefixes.
"""

NO_DATA_COLUMNS: typing.Final = frozenset({*MetricColumn, RUN_NAME_COLUMN})
"""Columns that say where a row is, not what a chart shows: present on every row whatever was logged."""

IDENTITY_COLUMNS: typing.Final = frozenset({MetricColumn.RUN_ID, RUN_NAME_COLUMN})
"""Which run a row belongs to, and what to call it: part of what every chart shows, though no chart asks for them."""

HPARAM_COLUMN_PREFIX: typing.Final = "hparam__"
"""Hyperparameter columns are merged into the same wide dataframe as metrics/artifacts, prefixed
so a hparam key can never collide with a same-named metric or artifact column."""

ARTIFACT_COLUMN_PREFIX: typing.Final = "artifact__"
"""Artifact columns are prefixed for the same reason -- see `artifact_column`/`artifact_tags_column`."""

_TAGS_SUFFIX: typing.Final = "__tags"
_FNAME_SUFFIX: typing.Final = "__fname"


def artifact_column(key: str) -> str:
    """The panel-dataframe column holding each step's download URL for artifact `key`."""
    return f"{ARTIFACT_COLUMN_PREFIX}{key}"


def artifact_tags_column(key: str) -> str:
    """The panel-dataframe column holding each step's tags dict for artifact `key`."""
    return f"{artifact_column(key)}{_TAGS_SUFFIX}"


def artifact_fname_column(key: str) -> str:
    """The panel-dataframe column holding each step's file name for artifact `key` (its URL carries none)."""
    return f"{artifact_column(key)}{_FNAME_SUFFIX}"


def artifact_keys_in(columns: Iterable[str], name: str, *, prefix: bool) -> list[str]:
    """
    The artifact keys among a panel dataframe's `columns` that `name` asks for, in column order.

    `name` is a key, or with `prefix` every key that starts with it. A key has one column of URLs and
    one each for its tags and file names, which are not keys of their own.
    """
    keys = [
        column.removeprefix(ARTIFACT_COLUMN_PREFIX)
        for column in columns
        if column.startswith(ARTIFACT_COLUMN_PREFIX) and not column.endswith((_TAGS_SUFFIX, _FNAME_SUFFIX))
    ]
    return [key for key in keys if (key.startswith(name) if prefix else key == name)]
