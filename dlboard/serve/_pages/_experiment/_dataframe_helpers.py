"""Dataframe-building and column-catalog helpers used only by the basic experiment page. No Dash."""

from __future__ import annotations

import typing
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from dlboard.models import Artifact, ColumnKind, HyperParams, MetricColumn
from dlboard.plugins.charts._table_style import HPARAM_COLUMN_PREFIX, artifact_column, artifact_tags_column
from dlboard.serve._backend._artifact_download import artifact_url
from dlboard.serve._pages._experiment._chart_autogen import default_artifact_chart

if typing.TYPE_CHECKING:
    from dlboard.models import DataStore

EXCLUDED_RUNS_KEY: typing.Final = "excluded_runs"
"""A `Page.page_settings` key -- the run ids the run-comparison table has deselected, which
`fetch_panel_dataframe` leaves out of every panel's fetch."""


def build_artifacts_dataframe(artifacts: typing.Iterable[Artifact]) -> pd.DataFrame:
    """Pivot artifact download URLs (and tags) into columns per key, indexed by (run_id, step)."""
    # A URL, not the underlying `ref` -- charts (and anything else consuming this dataframe) fetch
    # an artifact by id through `/artifact/<id>`, never by talking to the ref's backend directly.
    # One pass: each artifact is dumped and given its URL as it streams into the frame.
    df = pd.DataFrame(a.model_dump(mode="json") | {"url": artifact_url(a)} for a in artifacts)
    if df.empty:
        return df

    url_pivot = df.pivot_table(index=["run_id", "step"], columns="key", values="url", aggfunc="first")
    tags_pivot = df.pivot_table(index=["run_id", "step"], columns="key", values="tags", aggfunc="first")
    return (
        url_pivot.rename(columns=artifact_column)
        .join(tags_pivot.rename(columns=artifact_tags_column))
        .reset_index()
    )


def build_hyperparams_dataframe(
    hparams: typing.Iterable[HyperParams], keys: typing.Iterable[str] | None = None
) -> pd.DataFrame:
    """
    One row per run_id; hparam keys become `hparam__<key>`-prefixed columns.

    `keys`, if given, keeps only those hparam keys (client-side filter -- `fetch_hyperparams`
    doesn't support filtering server-side, unlike metrics).
    """
    wanted = set(keys) if keys is not None else None
    rows: list[dict[str, Any]] = []
    for h in hparams:
        values = h.hparams_dict
        if wanted is not None:
            values = {k: v for k, v in values.items() if k in wanted}
        rows.append({"run_id": h.run_id, **{f"{HPARAM_COLUMN_PREFIX}{k}": v for k, v in values.items()}})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame.from_records(rows)


def merge_hyperparams(df: pd.DataFrame, hparams_df: pd.DataFrame) -> pd.DataFrame:
    """Broadcast each run's (step-less) hparam values onto every row for that run_id."""
    if hparams_df.empty:
        return df
    if df.empty:
        return hparams_df
    return df.merge(hparams_df, on="run_id", how="left")


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


def merge_metrics_and_artifacts(metrics_df: pd.DataFrame, artifacts_df: pd.DataFrame) -> pd.DataFrame:
    if not metrics_df.empty and not artifacts_df.empty:
        return metrics_df.merge(artifacts_df, on=["run_id", "step"], how="outer")
    return metrics_df if not metrics_df.empty else artifacts_df
