"""Pure dataframe-building helpers used only by the basic experiment page. No Dash, no Page classes."""

from __future__ import annotations

import typing
from typing import Any

import pandas as pd

from dltrack.models import Artifact, ColumnKind, HyperParams, MetricColumn
from dltrack.plugins.charts._table_style import HPARAM_COLUMN_PREFIX, artifact_column, artifact_tags_column

EXCLUDED_RUNS_KEY: typing.Final = "excluded_runs"
"""A `Page.page_settings` key -- the run ids the run-comparison table has deselected, which
`fetch_panel_dataframe` leaves out of every panel's fetch."""


def build_artifacts_dataframe(artifacts: typing.Iterable[Artifact]) -> pd.DataFrame:
    """Pivot artifact refs (and tags) into columns per key, indexed by (run_id, step)."""
    df = pd.DataFrame([a.model_dump(mode="json") for a in artifacts])
    if df.empty:
        return df

    ref_pivot = df.pivot_table(index=["run_id", "step"], columns="key", values="ref", aggfunc="first")
    tags_pivot = df.pivot_table(index=["run_id", "step"], columns="key", values="tags", aggfunc="first")
    return (
        ref_pivot.rename(columns=artifact_column)
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


def infer_column_kinds(
    metric_columns: typing.Iterable[str],
    artifact_keys: typing.Iterable[str],
    hparam_keys: typing.Iterable[str] = (),
) -> dict[str, ColumnKind]:
    """
    Tag every known column with its kind.

    run_id is excluded (never a sensible field value);
    step is kept since it's a common x_axis choice.
    """
    kinds = {c: ColumnKind.METRIC for c in metric_columns if c != MetricColumn.RUN_ID}
    kinds.update(dict.fromkeys(artifact_keys, ColumnKind.ARTIFACT))
    kinds.update(dict.fromkeys(hparam_keys, ColumnKind.HPARAM))
    return kinds


def group_columns_by_kind(column_kinds: dict[str, str]) -> dict[ColumnKind, list[str]]:
    """
    Round-trip Store data (plain strings) back into ColumnKind-keyed groups.

    Also synthesizes a `GROUPING` bucket -- metric and hparam columns plus `run_id` -- for fields
    (e.g. a bar chart's `x_axis`) that group/split by any of those rather than being restricted to
    one specific kind.
    """
    grouped: dict[ColumnKind, list[str]] = {}
    for col, kind in column_kinds.items():
        grouped.setdefault(ColumnKind(kind), []).append(col)
    grouped[ColumnKind.GROUPING] = sorted(
        {"run_id", *grouped.get(ColumnKind.METRIC, []), *grouped.get(ColumnKind.HPARAM, [])}
    )
    return grouped


def single_value_metric_columns(df: pd.DataFrame, metric_columns: typing.Iterable[str]) -> set[str]:
    """Metric columns every run logs at most once -- a natural fit for a bar chart, not a line chart."""
    metric_columns = [c for c in metric_columns if c in df.columns]
    if df.empty or "run_id" not in df.columns or not metric_columns:
        return set()
    counts = df.groupby("run_id")[metric_columns].count()
    return {col for col in metric_columns if (counts[col] <= 1).all()}


def merge_metrics_and_artifacts(metrics_df: pd.DataFrame, artifacts_df: pd.DataFrame) -> pd.DataFrame:
    if not metrics_df.empty and not artifacts_df.empty:
        return metrics_df.merge(artifacts_df, on=["run_id", "step"], how="outer")
    return metrics_df if not metrics_df.empty else artifacts_df
