"""Every key an experiment's charts can be configured with, by kind -- what the chart editor and suggestions offer. No Dash."""

from __future__ import annotations

import functools
import itertools
import typing
from dataclasses import dataclass, field

from flask import has_app_context

from dlboard.models import (
    SUMMARY_RUNS,
    ColumnKind,
    MetricColumn,
    OverlayKey,
    layer_kind,
)
from dlboard.serve._backend._app_slot import AppSlot, current_dash_app
from dlboard.serve._pages._experiment._chart_autogen import default_artifact_chart
from dlboard.serve._pages._experiment._shown_runs import page_scope

if typing.TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from dlboard.models import DataStore
    from dlboard.serve._pages._experiment._kept_reads import KeptReads
    from dlboard.serve._pages._experiment._page_model import BasicExperimentPage


type _CatalogKey = tuple[int, int, frozenset[int]]
"""An experiment's id, its `revision`, and the runs of it a comparison holds: what its catalog was read from."""

COMPARISON_CATALOGS: AppSlot[KeptReads[_CatalogKey, ColumnCatalog]] = AppSlot("comparison column catalogs")
"""
The catalogs a comparison's experiments gave lately, which `ColumnCatalog.of_page` is answered from while none has changed.

A comparison reads one catalog for each experiment it holds runs of, a key picker on every keystroke.
"""

KEPT_CATALOGS: typing.Final = 64
"""How many experiments' catalogs a worker keeps: those of the comparisons being looked at on it at once."""

KEY_PICKER_LIMIT: typing.Final = 100
"""
How many names a key picker lists at once. It searches every name, and lists the first this many that match.
"""


def first_matches(names: Iterable[str], search: str, limit: int = KEY_PICKER_LIMIT) -> list[str]:
    """The first `limit` of `names` that have `search` in them, ignoring case: the page of them a key picker lists."""
    needle = search.lower()
    return list(itertools.islice((name for name in names if needle in name.lower()), max(limit, 0)))


def summary_hparam_keys(
    store: DataStore[...], experiment_id: int, runs: frozenset[int] | None = None
) -> list[str]:
    """
    The hyperparameter keys a picker offers: those of the newest `SUMMARY_RUNS` runs, the same bound as the metric keys'.

    Of `runs` alone, if given: a comparison offers the keys of the runs it holds, not of the experiment's newest.
    Never every run's: runs are unbounded, and every picker that lists keys would read them all, on each keystroke.
    """
    newest = frozenset(run.id for run in store.get_runs(experiment_id, limit=SUMMARY_RUNS, run_ids=runs))
    return sorted(
        {key for h in store.fetch_hyperparams(experiment_id, run_ids=newest) for key in h.hparams_dict}
    )


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
    def load(
        cls, store: DataStore[...], experiment_id: int, runs: frozenset[int] | None = None
    ) -> ColumnCatalog:
        """
        An experiment's names; its hyperparameter keys those of `runs` alone, if given (see `summary_hparam_keys`).

        The metrics, artifacts and histograms are the experiment's own: what its runs logged, which is what a key
        listing can say without reading them.
        """
        artifacts = store.summarize_artifact_keys(experiment_id)
        return cls(
            metrics=tuple(store.summarize_metric_keys(experiment_id)),
            artifacts=tuple(sorted({a.key for a in artifacts})),
            artifact_chart_types={
                a.key: chart_type for a in artifacts if (chart_type := default_artifact_chart(a)) is not None
            },
            layers=tuple(sorted({a.key for a in artifacts if layer_kind(a.tags, a.fname) is not None})),
            histograms=tuple(store.summarize_histogram_keys(experiment_id)),
            hparams=tuple(summary_hparam_keys(store, experiment_id, runs)),
        )

    @classmethod
    def of_comparison_part(
        cls, store: DataStore[...], experiment_id: int, runs: frozenset[int]
    ) -> ColumnCatalog:
        """
        What `load` gives for `runs` of an experiment, kept until a write to the experiment, which bumps its `revision`.

        Asks the caller's own store for the experiment first, as every read does, so what was kept for someone who
        may see it is never served to someone who may not.
        """
        experiment = store.get_experiment(experiment_id)
        kept = COMPARISON_CATALOGS.find(current_dash_app()) if has_app_context() else None
        if experiment is None or kept is None:
            return cls.load(store, experiment_id, runs)
        return kept.read(
            (experiment_id, experiment.revision, runs), lambda: cls.load(store, experiment_id, runs)
        )

    @classmethod
    def merged(cls, catalogs: Sequence[ColumnCatalog]) -> ColumnCatalog:
        """The names of every one of `catalogs`: what a comparison of several experiments' runs can be charted with."""
        if len(catalogs) == 1:
            return catalogs[0]

        def union(names: Iterable[tuple[str, ...]]) -> tuple[str, ...]:
            return tuple(sorted({name for group in names for name in group}))

        return cls(
            metrics=union(c.metrics for c in catalogs),
            artifacts=union(c.artifacts for c in catalogs),
            artifact_chart_types={
                key: kind for c in catalogs for key, kind in c.artifact_chart_types.items()
            },
            layers=union(c.layers for c in catalogs),
            histograms=union(c.histograms for c in catalogs),
            hparams=union(c.hparams for c in catalogs),
        )

    @classmethod
    def of_page(cls, store: DataStore[...], page: BasicExperimentPage) -> ColumnCatalog:
        """
        Every name `page`'s charts can be configured with: its experiment's, or the experiments' of the runs a comparison draws.

        A comparison's metrics are those of its experiments, not only of its members, and its hyperparameters those of
        its members alone.
        """
        if page.experiment_id is not None:
            return cls.load(store, page.experiment_id)
        return cls.merged(
            [
                cls.of_comparison_part(store, experiment_id, runs)
                for experiment_id, runs in page_scope(store, page).items()
            ]
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
    store: DataStore[...], experiment_ids: Iterable[int], keys: Iterable[str]
) -> frozenset[str]:
    """
    The `keys` every run logged at most once, in every one of `experiment_ids`: a natural fit for a bar chart, not a line chart.

    Judged by a probe per key and experiment, so a caller passes the keys it is about to chart and no others. A key one
    experiment logs over many steps is not single-valued, whatever the others do.
    """
    wanted = frozenset(keys)
    once, repeated = set[str](), set[str]()
    for experiment_id in experiment_ids:
        for summary in store.summarize_metric_steps(experiment_id, wanted):
            (once if summary.max_steps_per_run == 1 else repeated).add(summary.key)
    return frozenset(once - repeated)
