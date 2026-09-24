"""
Pure helpers for turning metric/artifact key naming conventions into chart panels.

Lightning-style keys look like `split/metric_name/threshold` (or `split_metric_name_threshold`).
Splitting on a delimiter and grouping by the first (prefix) or last (suffix) segment gives a
decent default panel layout for a brand-new experiment, and the same grouping logic powers
"suggest a chart for this uncharted key" once the user has started customizing the view.
"""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING, Literal

from dltrack.models import ChartInstance, ColumnKind, PanelInstance
from dltrack.plugins.charts.bar_chart import BarChart
from dltrack.plugins.charts.image_series import ImageChart
from dltrack.plugins.charts.line_chart import LineChart

if TYPE_CHECKING:
    from collections.abc import Mapping

SplitMode = Literal["prefix", "suffix"]

# Bookkeeping columns that ride along with every metric row; never sensible to chart directly.
_METRIC_BOOKKEEPING_COLS = frozenset({"run_id", "step", "index", "timestamp_utc", "experiment_id"})

# Artifact-derived panels always get this suffix, so they can never collide with a same-named
# metric panel even when the prefix/suffix grouping produces the same group name for both.
ARTIFACT_PANEL_SUFFIX = " (artifacts)"

# Keys the delimiter doesn't actually split (no delimiter configured, or the key doesn't contain
# it) all land together in this one shared panel, rather than each getting its own single-chart
# panel.
UNGROUPED_GROUP_NAME = "Ungrouped"

# Suffixes `pytorch_lightning` appends to a logged metric's name when it's aggregated per-epoch or
# reported per-step (e.g. `self.log(name, ..., on_step=True, on_epoch=True)` ships both
# `<name>_step` and `<name>_epoch`). Distinct from the user-configurable delimiter/mode grouping
# above -- these are fixed, and only ever applied for experiments logged through
# `DLTrackLogger` (see `ExperimentSource.PYTORCH_LIGHTNING`).
_LIGHTNING_GRANULARITY_SUFFIXES: typing.Final = {"_epoch": "epoch", "_step": "step"}


def lightning_granularity(key: str) -> str | None:
    """The lightning step/epoch granularity `key` carries, if any, e.g. `"epoch"` for `loss_epoch`."""
    for suffix, granularity in _LIGHTNING_GRANULARITY_SUFFIXES.items():
        if key.endswith(suffix) and len(key) > len(suffix):
            return granularity
    return None


def split_group_name(key: str, delimiter: str, mode: SplitMode) -> str:
    """
    The panel-group name for `key`: its first (prefix) or last (suffix) delimited segment.

    Keys with nothing to split on (no delimiter configured, or the delimiter isn't present in
    the key) all collapse to `UNGROUPED_GROUP_NAME` instead of each getting their own panel.
    """
    if not delimiter:
        return UNGROUPED_GROUP_NAME
    parts = [p for p in key.split(delimiter) if p]
    if len(parts) <= 1:
        return UNGROUPED_GROUP_NAME
    return parts[0] if mode == "prefix" else parts[-1]


def panel_name_for_group(group: str, kind: ColumnKind, *, granularity: str | None = None) -> str:
    """
    The target panel name for a group, disambiguated by kind so metric/artifact panels never collide.

    `granularity` (a lightning step/epoch tag from `lightning_granularity`) further splits the
    group's panel in two, so step-logged and epoch-logged variants of the same metric land in
    adjacent but distinct panels instead of competing for one.
    """
    name = f"{group}{ARTIFACT_PANEL_SUFFIX}" if kind == ColumnKind.ARTIFACT else group
    return f"{name} ({granularity})" if granularity else name


def default_chart_for_metric(
    column: str, *, single_value: bool = False
) -> ChartInstance[typing.Any, typing.Any]:
    """
    The sensible default chart for a metric column: a line chart against `step`.

    `single_value` (every run logs this metric at most once, per
    `_dataframe_helpers.single_value_metric_columns`) switches that to a bar chart comparing the
    value across runs instead -- a line chart of a single point per run isn't a chart at all.
    """
    if single_value:
        return ChartInstance(chart_type=BarChart.name, parameters={"column": column, "x_axis": "run_id"})
    return ChartInstance(chart_type=LineChart.name, parameters={"column": column, "x_axis": "step"})


def default_chart_for_artifact(key: str) -> ChartInstance[typing.Any, typing.Any]:
    """The sensible default chart for an artifact key: an image series (x_axis defaults to `step`)."""
    return ChartInstance(chart_type=ImageChart.name, parameters={"key": key})


def chartable_metric_columns(column_kinds: Mapping[str, str]) -> list[str]:
    """Metric columns worth offering a chart for (i.e. not step/run_id/etc bookkeeping)."""
    return sorted(
        col
        for col, kind in column_kinds.items()
        if ColumnKind(kind) == ColumnKind.METRIC and col not in _METRIC_BOOKKEEPING_COLS
    )


def artifact_keys(column_kinds: Mapping[str, str]) -> list[str]:
    """Artifact keys available for this experiment."""
    return sorted(col for col, kind in column_kinds.items() if ColumnKind(kind) == ColumnKind.ARTIFACT)


def build_auto_panels(
    column_kinds: Mapping[str, str],
    *,
    delimiter: str,
    mode: SplitMode,
    lightning: bool = False,
    single_value_columns: frozenset[str] = frozenset(),
) -> list[PanelInstance[typing.Any, typing.Any]]:
    """
    Build a full set of panels from every known metric/artifact key, grouped by `delimiter`/`mode`.

    Intended for a brand-new (empty) view: metric keys and artifact keys are grouped and charted
    independently, so a metric group and an artifact group with the same name still land in two
    distinct panels (see `panel_name_for_group`). `lightning` further splits each metric group by
    step/epoch granularity (see `lightning_granularity`) -- only meaningful for experiments logged
    through `DLTrackLogger`, so callers should gate it on `ExperimentSource.PYTORCH_LIGHTNING`.
    """
    panels: dict[str, PanelInstance[typing.Any, typing.Any]] = {}

    def _append(panel_name: str, chart: ChartInstance[typing.Any, typing.Any]) -> None:
        existing = panels.get(panel_name)
        if existing is None:
            panels[panel_name] = PanelInstance(name=panel_name, charts=[chart])
        else:
            panels[panel_name] = existing.model_copy(update={"charts": [*existing.charts, chart]})

    for column in chartable_metric_columns(column_kinds):
        group = split_group_name(column, delimiter, mode)
        granularity = lightning_granularity(column) if lightning else None
        panel_name = panel_name_for_group(group, ColumnKind.METRIC, granularity=granularity)
        _append(panel_name, default_chart_for_metric(column, single_value=column in single_value_columns))

    for key in artifact_keys(column_kinds):
        group = split_group_name(key, delimiter, mode)
        _append(panel_name_for_group(group, ColumnKind.ARTIFACT), default_chart_for_artifact(key))

    return list(panels.values())


class UnchartedKeys(typing.NamedTuple):
    """Metric columns and artifact keys not yet referenced by any chart on the page."""

    metrics: list[str]
    artifacts: list[str]


def find_uncharted_keys(
    panels: typing.Iterable[PanelInstance[typing.Any, typing.Any]],
    column_kinds: Mapping[str, str],
) -> UnchartedKeys:
    """Diff every known metric/artifact key against what's already charted somewhere on the page."""
    charted_metrics: set[str] = set()
    charted_artifacts: set[str] = set()
    for panel in panels:
        hinted_columns = panel.hint_required_columns()
        if hinted_columns is not None:
            charted_metrics |= hinted_columns
        charted_artifacts |= {k for k in panel.hint_required_artifact_keys() if k is not None}

    return UnchartedKeys(
        metrics=[c for c in chartable_metric_columns(column_kinds) if c not in charted_metrics],
        artifacts=[k for k in artifact_keys(column_kinds) if k not in charted_artifacts],
    )


class Suggestion(typing.NamedTuple):
    """One uncharted key, with the chart + target panel name it would get if added."""

    key: str
    kind: ColumnKind
    panel_name: str
    chart: ChartInstance[typing.Any, typing.Any]


def build_suggestions(
    uncharted: UnchartedKeys,
    *,
    delimiter: str,
    mode: SplitMode,
    lightning: bool = False,
    single_value_columns: frozenset[str] = frozenset(),
) -> list[Suggestion]:
    """Turn uncharted keys into ready-to-add `Suggestion`s, grouped the same way as auto-populate."""
    suggestions = [
        Suggestion(
            key=column,
            kind=ColumnKind.METRIC,
            panel_name=panel_name_for_group(
                split_group_name(column, delimiter, mode),
                ColumnKind.METRIC,
                granularity=lightning_granularity(column) if lightning else None,
            ),
            chart=default_chart_for_metric(column, single_value=column in single_value_columns),
        )
        for column in uncharted.metrics
    ]
    suggestions.extend(
        Suggestion(
            key=key,
            kind=ColumnKind.ARTIFACT,
            panel_name=panel_name_for_group(split_group_name(key, delimiter, mode), ColumnKind.ARTIFACT),
            chart=default_chart_for_artifact(key),
        )
        for key in uncharted.artifacts
    )
    return suggestions
