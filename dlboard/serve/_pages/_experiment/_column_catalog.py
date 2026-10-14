"""Every key an experiment's charts can be configured with, by kind -- what the chart editor and suggestions offer. No Dash."""

from __future__ import annotations

import typing
from dataclasses import dataclass, field

from dlboard.models import (
    ColumnKind,
    MetricColumn,
)
from dlboard.serve._pages._experiment._chart_autogen import default_artifact_chart

if typing.TYPE_CHECKING:
    from dlboard.models import DataStore


@dataclass(frozen=True)
class ColumnCatalog:
    """
    Every name an experiment's charts can be configured with, by kind -- no values, just keys.

    What the chart editor offers as field choices, and what auto-populate/suggest-charts build
    charts from. Loaded fresh (a few key-listing queries) whenever it's needed, never cached
    client-side: it's cheap, and a cached copy goes stale the moment training logs a new key.
    """

    metrics: tuple[str, ...] = ()
    single_value_metrics: frozenset[str] = frozenset()
    """Metrics every run logged at most once -- a natural fit for a bar chart, not a line chart."""
    artifacts: tuple[str, ...] = ()
    artifact_chart_types: dict[str, str] = field(default_factory=dict[str, str])
    """The chart type that displays each artifact key by default, by name. A key no chart type can display is absent."""
    hparams: tuple[str, ...] = ()

    @classmethod
    def load(cls, store: DataStore[...], experiment_id: int) -> ColumnCatalog:
        metrics = store.summarize_metric_keys(experiment_id)
        artifacts = list(store.fetch_artifacts(experiment_id))
        return cls(
            metrics=tuple(m.key for m in metrics),
            single_value_metrics=frozenset(m.key for m in metrics if m.max_steps_per_run <= 1),
            artifacts=tuple(sorted({a.key for a in artifacts})),
            artifact_chart_types={
                a.key: chart_type for a in artifacts if (chart_type := default_artifact_chart(a)) is not None
            },
            hparams=tuple(
                sorted({k for h in store.fetch_hyperparams(experiment_id) for k in h.hparams_dict})
            ),
        )

    @property
    def has_chartable_keys(self) -> bool:
        return bool(self.metrics or self.artifact_chart_types)

    def options(self, kind: ColumnKind) -> list[str]:
        """The choices for a chart field of `kind` -- `step`/`timestamp_utc` being common metric x-axes."""
        match kind:
            case ColumnKind.METRIC:
                return [*self.metrics, MetricColumn.STEP, MetricColumn.TIMESTAMP_UTC] if self.metrics else []
            case ColumnKind.ARTIFACT:
                return list(self.artifacts)
            case ColumnKind.HPARAM:
                return list(self.hparams)
            case ColumnKind.GROUPING:
                return sorted({MetricColumn.RUN_ID, *self.options(ColumnKind.METRIC), *self.hparams})
