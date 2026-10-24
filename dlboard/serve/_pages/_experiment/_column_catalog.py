"""Every key an experiment's charts can be configured with, by kind -- what the chart editor and suggestions offer. No Dash."""

from __future__ import annotations

import functools
import typing
from dataclasses import dataclass, field

from dlboard.models import (
    SUMMARY_RUNS,
    ColumnKind,
    MetricColumn,
    OverlayKey,
    layer_kind,
)
from dlboard.serve._pages._experiment._chart_autogen import default_artifact_chart

if typing.TYPE_CHECKING:
    from dlboard.models import DataStore


KEY_PICKER_LIMIT: typing.Final = 100
"""
How many names a key picker lists at once. It searches every name, and lists the first this many that match.
"""


@dataclass(frozen=True)
class ColumnCatalog:
    """
    Every name an experiment's charts can be configured with, by kind -- no values, just keys.

    What the chart editor offers as field choices, and what auto-populate/suggest-charts build
    charts from. Loaded fresh (a few key-listing queries) whenever it's needed, never cached
    client-side: it's cheap, and a cached copy goes stale the moment training logs a new key.
    """

    metrics: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    artifact_chart_types: dict[str, str] = field(default_factory=dict[str, str])
    """The chart type that displays each artifact key by default, by name. A key no chart type can display is absent."""
    layers: tuple[str, ...] = ()
    """Artifact keys an image chart can draw over a picture: those of `Overlay`s, and of images."""
    histograms: tuple[str, ...] = ()
    hparams: tuple[str, ...] = ()

    @classmethod
    def load(cls, store: DataStore[...], experiment_id: int) -> ColumnCatalog:
        artifacts = store.summarize_artifact_keys(experiment_id)
        # Hyperparameter keys are read from the newest runs, the same bound as the metric keys' (`SUMMARY_RUNS`).
        newest = frozenset(run.id for run in store.get_runs(experiment_id, limit=SUMMARY_RUNS, run_ids=None))
        return cls(
            metrics=tuple(store.summarize_metric_keys(experiment_id)),
            artifacts=tuple(sorted({a.key for a in artifacts})),
            artifact_chart_types={
                a.key: chart_type for a in artifacts if (chart_type := default_artifact_chart(a)) is not None
            },
            layers=tuple(sorted({a.key for a in artifacts if layer_kind(a.tags, a.fname) is not None})),
            histograms=tuple(store.summarize_histogram_keys(experiment_id)),
            hparams=tuple(
                sorted(
                    {
                        k
                        for h in store.fetch_hyperparams(experiment_id, run_ids=newest)
                        for k in h.hparams_dict
                    }
                )
            ),
        )

    @property
    def has_chartable_keys(self) -> bool:
        return bool(self.metrics or self.artifact_chart_types or self.histograms)

    def artifact_keys(self) -> list[str]:
        return sorted(self.artifacts)

    @functools.cached_property
    def _drawn_over_images(self) -> frozenset[str]:
        """The artifact keys that are an overlay of an image that is logged too: its chart draws them, so they are no choice."""
        keys = frozenset(self.artifacts)
        return frozenset(k for k in keys if (overlay := OverlayKey.parse(k)) and overlay.image_key in keys)

    @functools.cached_property
    def _pictures_with_overlays(self) -> frozenset[str]:
        """The keys of pictures that have overlays logged over them: a picture the chart is of, not a layer to add to it."""
        overlays = (OverlayKey.parse(key) for key in self.artifacts)
        return frozenset(overlay.image_key for overlay in overlays if overlay is not None)

    def options(self, kind: ColumnKind) -> list[str]:
        """The choices for a chart field of `kind` -- `step`/`timestamp_utc` being common metric x-axes."""
        match kind:
            case ColumnKind.METRIC:
                return [*self.metrics, MetricColumn.STEP, MetricColumn.TIMESTAMP_UTC] if self.metrics else []
            case ColumnKind.ARTIFACT:
                return [key for key in self.artifacts if key not in self._drawn_over_images]
            case ColumnKind.LAYER:
                return [
                    key
                    for key in self.layers
                    if OverlayKey.parse(key) is None and key not in self._pictures_with_overlays
                ]
            case ColumnKind.HISTOGRAM:
                return list(self.histograms)
            case ColumnKind.HPARAM:
                return list(self.hparams)
            case ColumnKind.SERIES_BAND:
                msg = f"no field is populated from a {kind} column"
                raise ValueError(msg)
            case ColumnKind.GROUPING:
                return sorted({MetricColumn.RUN_ID, *self.options(ColumnKind.METRIC), *self.hparams})


def single_value_metrics(
    store: DataStore[...], experiment_id: int, keys: typing.Iterable[str]
) -> frozenset[str]:
    """
    The `keys` every run logged at most once: a natural fit for a bar chart, not a line chart.

    Judged by a probe per key, so a caller passes the keys it is about to chart and no others.
    """
    steps = store.summarize_metric_steps(experiment_id, frozenset(keys))
    return frozenset(s.key for s in steps if s.max_steps_per_run == 1)
