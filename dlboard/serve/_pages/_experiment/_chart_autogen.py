"""
Pure helpers for turning metric/artifact key naming conventions into chart panels.

Lightning-style keys look like `split/metric_name/threshold` (or `split_metric_name_threshold`).
Splitting on a delimiter and grouping by the first (prefix) or last (suffix) segment gives a
decent default panel layout for a brand-new experiment, and the same grouping logic powers
"suggest a chart for this uncharted key" once the user has started customizing the view.
"""

from __future__ import annotations

import typing
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from dlboard.models import OVERLAY_KEY_SUFFIX, Artifact, ChartInstance, ChartType, ColumnKind, PanelInstance
from dlboard.plugins.charts.bar_chart import BarChart
from dlboard.plugins.charts.file_list import FileListChart
from dlboard.plugins.charts.histogram import HistogramChart
from dlboard.plugins.charts.image_series import ImageChart, OverlayLayer
from dlboard.plugins.charts.line_chart import LineChart

if TYPE_CHECKING:
    from dlboard.serve._pages._experiment._column_catalog import ColumnCatalog

SplitMode = Literal["prefix", "suffix"]

# Artifact-derived panels always get this suffix, so they can never collide with a same-named
# metric panel even when the prefix/suffix grouping produces the same group name for both.
ARTIFACT_PANEL_SUFFIX = " (artifacts)"
HISTOGRAM_PANEL_SUFFIX = " (histograms)"
"""The same, for histograms."""

# Keys the delimiter doesn't actually split (no delimiter configured, or the key doesn't contain
# it) all land together in this one shared panel, rather than each getting its own single-chart
# panel.
AUTO_OPEN_MAX_CHARTS: typing.Final = 12
"""
The most charts a panel can hold and still be opened for you by auto-generate. Mounting a panel costs
about 40 ms per chart in the browser, so a panel of a hundred stays closed -- its header says how many
charts it holds -- until you open it, instead of making the whole page wait for it.
"""

UNGROUPED_GROUP_NAME = "Ungrouped"

# Suffixes `pytorch_lightning` appends to a logged metric's name when it's aggregated per-epoch or
# reported per-step (e.g. `self.log(name, ..., on_step=True, on_epoch=True)` ships both
# `<name>_step` and `<name>_epoch`). Distinct from the user-configurable delimiter/mode grouping
# above -- these are fixed, and only ever applied for experiments logged through
# `DLBoardLogger` (see `ExperimentSource.PYTORCH_LIGHTNING`).
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
    The target panel name for a group, disambiguated by kind so metric/artifact/histogram panels never collide.

    `granularity` (a lightning step/epoch tag from `lightning_granularity`) further splits the
    group's panel in two, so step-logged and epoch-logged variants of the same metric land in
    adjacent but distinct panels instead of competing for one.
    """
    match kind:
        case ColumnKind.ARTIFACT:
            name = f"{group}{ARTIFACT_PANEL_SUFFIX}"
        case ColumnKind.HISTOGRAM:
            name = f"{group}{HISTOGRAM_PANEL_SUFFIX}"
        case ColumnKind.METRIC | ColumnKind.HPARAM | ColumnKind.GROUPING:
            name = group
    return f"{name} ({granularity})" if granularity else name


def default_chart_for_metric(
    column: str, *, single_value: bool = False
) -> ChartInstance[typing.Any, typing.Any]:
    """
    The sensible default chart for a metric column: a line chart against `step`.

    `single_value` (every run logs this metric at most once, per
    `ColumnCatalog.single_value_metrics`) switches that to a bar chart comparing the
    value across runs instead -- a line chart of a single point per run isn't a chart at all.
    """
    if single_value:
        return ChartInstance(chart_type=BarChart.name, parameters={"column": column, "x_axis": "run_id"})
    return ChartInstance(chart_type=LineChart.name, parameters={"column": column, "x_axis": "step"})


def default_chart_for_histogram(key: str) -> ChartInstance[typing.Any, typing.Any]:
    """The default chart for a histogram key: its heatmap, the view that shows the whole distribution over steps."""
    return ChartInstance(chart_type=HistogramChart.name, parameters={"key": key})


ARTIFACT_CHART_TYPES: typing.Final[tuple[type[ChartType[typing.Any, typing.Any, typing.Any]], ...]] = (
    FileListChart,
    ImageChart,
)
"""
The built-in chart types auto-generate may give an artifact key, most specific first: a file is marked
as one by its tag, which a file named like an image could otherwise be mistaken for.
"""


def default_artifact_chart(artifact: Artifact) -> str | None:
    """
    The name of the chart type that displays `artifact` by default, or `None` if none of the built-in ones can.

    Each chart type says what it can display (`ChartType.can_display_artifact`), so an artifact no
    chart type claims gets no generated chart instead of one that can't show it.
    """
    return next((c.name for c in ARTIFACT_CHART_TYPES if c.can_display_artifact(artifact)), None)


def default_chart_for_artifact(
    key: str, chart_type: str, overlay_keys: typing.Sequence[str] = ()
) -> ChartInstance[typing.Any, typing.Any]:
    """
    The default chart for an artifact key, of the chart type `default_artifact_chart` chose for it.

    An image chart draws each of `overlay_keys` over its pictures, at full opacity.
    """
    parameters: dict[str, typing.Any] = {"key": key}
    if overlay_keys:
        parameters["overlays"] = {k: OverlayLayer().model_dump() for k in overlay_keys}
    return ChartInstance(chart_type=chart_type, parameters=parameters)


class ArtifactChart(typing.NamedTuple):
    """A default chart for some artifact keys, and what to call it."""

    label: str
    """The key it charts, or for a directory of files the prefix it charts (`checkpoints/`)."""
    group_key: str
    """One real key it covers, which decides the panel it lands in (`split_group_name`)."""
    chart: ChartInstance[typing.Any, typing.Any]


def default_artifact_charts(keys: typing.Iterable[str], catalog: ColumnCatalog) -> list[ArtifactChart]:
    """
    The default charts for artifact `keys`: one each, except that files in one directory share a chart.

    Files are logged one key apiece (`checkpoints/epoch=3-step=300.ckpt`), so a long run gives hundreds
    of keys that are really one list. A single chart over the directory (`checkpoints/`) shows them
    together, grouped and paged, and includes any logged later.

    An image's `OVERLAY_KEY_SUFFIX` artifact, if there is one, is drawn over it by its chart rather than charted on
    its own.
    """
    keys = list(keys)
    overlay_of = {
        key: overlay
        for key in keys
        if catalog.artifact_chart_types[key] == ImageChart.name
        and (overlay := f"{key}{OVERLAY_KEY_SUFFIX}") in catalog.artifacts
    }
    drawn_over_images = set(overlay_of.values())
    charts: list[ArtifactChart] = []
    directories: set[str] = set()
    for key in keys:
        if key in drawn_over_images:
            continue
        chart_type = catalog.artifact_chart_types[key]
        directory = key.rpartition("/")[0]
        if chart_type != FileListChart.name or not directory:
            overlays: list[str] = [overlay_of[key]] if key in overlay_of else []
            charts.append(ArtifactChart(key, key, default_chart_for_artifact(key, chart_type, overlays)))
        elif (prefix := f"{directory}/") not in directories:
            directories.add(prefix)
            charts.append(
                ArtifactChart(
                    prefix, key, ChartInstance(chart_type=chart_type, parameters={"key_prefix": prefix})
                )
            )
    return charts


def group_keys_into_panels(
    catalog: ColumnCatalog,
    *,
    delimiter: str,
    mode: SplitMode,
    lightning: bool = False,
) -> dict[str, list[tuple[str, ColumnKind]]]:
    """
    Every known metric/artifact key, grouped by `delimiter`/`mode` under the panel it'd land in.

    Metric keys and artifact keys are grouped independently, so a metric group and an artifact
    group with the same name still land in two distinct panels (see `panel_name_for_group`).
    `lightning` further splits each metric group by step/epoch granularity (see
    `lightning_granularity`) -- only meaningful for experiments logged through `DLBoardLogger`, so
    callers should gate it on `ExperimentSource.PYTORCH_LIGHTNING`.
    """
    panels: dict[str, list[tuple[str, ColumnKind]]] = {}
    for column in catalog.metrics:
        granularity = lightning_granularity(column) if lightning else None
        group = split_group_name(column, delimiter, mode)
        panel_name = panel_name_for_group(group, ColumnKind.METRIC, granularity=granularity)
        panels.setdefault(panel_name, []).append((column, ColumnKind.METRIC))
    for artifact in default_artifact_charts(catalog.artifact_chart_types, catalog):
        group = split_group_name(artifact.group_key, delimiter, mode)
        panels.setdefault(panel_name_for_group(group, ColumnKind.ARTIFACT), []).append(
            (artifact.label, ColumnKind.ARTIFACT)
        )
    for key in catalog.histograms:
        group = split_group_name(key, delimiter, mode)
        panels.setdefault(panel_name_for_group(group, ColumnKind.HISTOGRAM), []).append(
            (key, ColumnKind.HISTOGRAM)
        )
    return panels


def panel_to_open(panels: typing.Sequence[PanelInstance[typing.Any, typing.Any]]) -> list[str]:
    """The panels auto-generate leaves open: the first one small enough to render at once, or none."""
    return [p.name for p in panels if len(p.charts) <= AUTO_OPEN_MAX_CHARTS][:1]


def build_auto_panels(
    catalog: ColumnCatalog,
    *,
    delimiter: str,
    mode: SplitMode,
    lightning: bool = False,
) -> list[PanelInstance[typing.Any, typing.Any]]:
    """A full set of panels for a brand-new (empty) view: one default chart per key, grouped as `group_keys_into_panels`."""
    artifact_charts = {
        a.label: a.chart for a in default_artifact_charts(catalog.artifact_chart_types, catalog)
    }

    def chart_for(key: str, kind: ColumnKind) -> ChartInstance[typing.Any, typing.Any]:
        match kind:
            case ColumnKind.METRIC:
                return default_chart_for_metric(key, single_value=key in catalog.single_value_metrics)
            case ColumnKind.ARTIFACT:
                return artifact_charts[key]
            case ColumnKind.HISTOGRAM:
                return default_chart_for_histogram(key)
            case ColumnKind.HPARAM | ColumnKind.GROUPING:
                msg = f"no chart is generated for a {kind} key"
                raise ValueError(msg)

    return [
        PanelInstance(
            name=panel_name,
            charts=[chart_for(key, kind) for key, kind in keys],
        )
        for panel_name, keys in group_keys_into_panels(
            catalog, delimiter=delimiter, mode=mode, lightning=lightning
        ).items()
    ]


class UnchartedKeys(typing.NamedTuple):
    """Metric columns, artifact keys and histogram keys not yet referenced by any chart on the page."""

    metrics: list[str]
    artifacts: list[str]
    histograms: list[str] = []  # noqa: RUF012 -- a NamedTuple field default; never mutated


def find_uncharted_keys(
    panels: typing.Iterable[PanelInstance[typing.Any, typing.Any]], catalog: ColumnCatalog
) -> UnchartedKeys:
    """Diff every known metric/artifact key against what's already charted somewhere on the page."""
    charted_metrics: set[str] = set()
    charted_artifacts: set[str] = set()
    charted_prefixes: set[str] = set()
    charted_histograms: set[str] = set()
    for panel in panels:
        hinted_columns = panel.hint_required_columns()
        if hinted_columns is not None:
            charted_metrics |= hinted_columns
        charted_artifacts |= {k for k in panel.hint_required_artifact_keys() if k is not None}
        charted_prefixes |= panel.hint_required_artifact_key_prefixes()
        charted_histograms |= panel.hint_required_histogram_keys()

    return UnchartedKeys(
        metrics=[c for c in catalog.metrics if c not in charted_metrics],
        artifacts=[
            k
            for k in catalog.artifact_chart_types
            if k not in charted_artifacts and not k.startswith(tuple(charted_prefixes))
        ],
        histograms=[k for k in catalog.histograms if k not in charted_histograms],
    )


class Suggestion(typing.NamedTuple):
    """One uncharted key, with the chart + target panel name it would get if added."""

    key: str
    kind: ColumnKind
    panel_name: str
    chart: ChartInstance[typing.Any, typing.Any]

    @property
    def selection_value(self) -> str:
        """What a checkbox for this suggestion carries, and `parse_selection` reads back."""
        return f"{self.kind.value}:{self.key}"


class SuggestSort(StrEnum):
    """How the suggestions list is ordered."""

    NAME = "name"
    PANEL = "panel"


def parse_selection(values: typing.Iterable[str]) -> frozenset[tuple[ColumnKind, str]]:
    """The (kind, key) pairs behind checkbox values made by `Suggestion.selection_value`."""
    return frozenset((ColumnKind(kind), key) for kind, _, key in (value.partition(":") for value in values))


def visible_suggestions(
    suggestions: typing.Iterable[Suggestion], *, text: str, sort: SuggestSort
) -> list[Suggestion]:
    """The suggestions whose key or target panel contains `text` (ignoring case), in `sort` order."""
    needle = text.strip().lower()
    matching = (s for s in suggestions if needle in s.key.lower() or needle in s.panel_name.lower())
    match sort:
        case SuggestSort.NAME:
            return sorted(matching, key=lambda s: s.key)
        case SuggestSort.PANEL:
            return sorted(matching, key=lambda s: (s.panel_name, s.key))


def build_suggestions(
    uncharted: UnchartedKeys,
    catalog: ColumnCatalog,
    *,
    delimiter: str,
    mode: SplitMode,
    lightning: bool = False,
) -> list[Suggestion]:
    """
    Turn uncharted keys into ready-to-add `Suggestion`s, grouped the same way as auto-populate.

    `catalog` says which metrics are single-valued (a bar chart) and which chart type displays each
    artifact key, exactly as it does for `build_auto_panels`.
    """
    suggestions = [
        Suggestion(
            key=column,
            kind=ColumnKind.METRIC,
            panel_name=panel_name_for_group(
                split_group_name(column, delimiter, mode),
                ColumnKind.METRIC,
                granularity=lightning_granularity(column) if lightning else None,
            ),
            chart=default_chart_for_metric(column, single_value=column in catalog.single_value_metrics),
        )
        for column in uncharted.metrics
    ]
    suggestions.extend(
        Suggestion(
            key=artifact.label,
            kind=ColumnKind.ARTIFACT,
            panel_name=panel_name_for_group(
                split_group_name(artifact.group_key, delimiter, mode), ColumnKind.ARTIFACT
            ),
            chart=artifact.chart,
        )
        for artifact in default_artifact_charts(uncharted.artifacts, catalog)
    )
    suggestions.extend(
        Suggestion(
            key=key,
            kind=ColumnKind.HISTOGRAM,
            panel_name=panel_name_for_group(split_group_name(key, delimiter, mode), ColumnKind.HISTOGRAM),
            chart=default_chart_for_histogram(key),
        )
        for key in uncharted.histograms
    )
    return suggestions
