"""
How a panel dataframe tells its own columns apart from a key logged under one, and so what no such key may be spelled as.

A metric is a column of its own name, so one starting like an artifact's or a hyperparameter's column would be read as
one; an artifact's tags and file names are columns of its key and a suffix, so a key ending in one would be read as
another artifact's. `LoggedMetrics` and `NewArtifact` refuse such keys where they are made (#774).
"""

from __future__ import annotations

import typing

HPARAM_COLUMN_PREFIX: typing.Final = "hparam__"
"""Hyperparameter columns are merged into the same wide dataframe as metrics/artifacts, prefixed
so a hparam key can never collide with a same-named metric or artifact column."""

ARTIFACT_COLUMN_PREFIX: typing.Final = "artifact__"
"""Artifact columns are prefixed for the same reason -- see `artifact_column`/`artifact_tags_column`."""

HISTOGRAM_COLUMN_PREFIX: typing.Final = "histogram__"
"""Histogram columns are prefixed for the same reason; each cell holds a `Histogram`, or NaN where the key was not logged."""

SERIES_BAND_COLUMN_PREFIX: typing.Final = "series_band__"
"""Series-band columns are prefixed for the same reason; each cell holds a `SeriesBand`, or NaN where the grid has no step."""

COLUMN_PREFIXES: typing.Final = (
    HPARAM_COLUMN_PREFIX,
    ARTIFACT_COLUMN_PREFIX,
    HISTOGRAM_COLUMN_PREFIX,
    SERIES_BAND_COLUMN_PREFIX,
)
"""What starts a column that is not a metric's: no metric key may start with one."""

ARTIFACT_TAGS_SUFFIX: typing.Final = "__tags"
ARTIFACT_FNAME_SUFFIX: typing.Final = "__fname"
ARTIFACT_LOGGED_SUFFIX: typing.Final = "__logged"
ARTIFACT_COLUMN_SUFFIXES: typing.Final = (ARTIFACT_TAGS_SUFFIX, ARTIFACT_FNAME_SUFFIX, ARTIFACT_LOGGED_SUFFIX)
"""What ends an artifact's column that is not its own (its tags, file names, steps): no artifact key may end with one."""


def misread_metric_keys(keys: typing.Iterable[str]) -> list[str]:
    """The `keys` a panel dataframe would read as another kind of column than a metric's, sorted."""
    return sorted(key for key in keys if key.startswith(COLUMN_PREFIXES))


def is_misread_artifact_key(key: str) -> bool:
    """Whether a panel dataframe would read artifact `key`'s column as another artifact's tags, files or steps."""
    return key.endswith(ARTIFACT_COLUMN_SUFFIXES)
