# pyright: reportPrivateUsage=false
"""Tests for the pure dataframe-building helpers used by experiment pages."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from dltrack.models import Artifact, LoggedMetrics
from dltrack.models._view import ColumnKind
from dltrack.plugins.pages._dataframe_helpers import (
    build_artifacts_dataframe,
    build_metrics_dataframe,
    filter_excluded_runs,
    group_columns_by_kind,
    infer_column_kinds,
    merge_metrics_and_artifacts,
)

_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _metric(run_id: int, step: int, **metrics: float | None) -> LoggedMetrics:
    return LoggedMetrics(metrics=metrics, step=step, experiment_id=1, run_id=run_id, timestamp_utc=_TS)


def _artifact(run_id: int, step: int, key: str, ref: str, tags: dict[str, str] | None = None) -> Artifact:
    return Artifact(
        key=key, fname=f"{key}.bin", run_id=run_id, experiment_id=1, step=step, ref=ref, tags=tags or {}
    )


def test_build_metrics_dataframe_merges_differing_keys_and_indexes() -> None:
    df = build_metrics_dataframe(
        [_metric(1, 0, loss=0.5), _metric(1, 1, loss=0.4, acc=0.9)],
    )
    assert set(df.columns) == {"loss", "acc", "step", "experiment_id", "run_id", "timestamp_utc", "index"}
    assert pd.isna(df.loc[df["step"] == 0, "acc"]).all()
    assert df.loc[df["step"] == 1, "acc"].iloc[0] == pytest.approx(0.9)


def test_build_metrics_dataframe_empty() -> None:
    assert build_metrics_dataframe([]).empty


def test_build_artifacts_dataframe_pivots_ref_and_tags() -> None:
    df = build_artifacts_dataframe(
        [_artifact(1, 0, "img", "ref://a", tags={"split": "train"}), _artifact(1, 1, "img", "ref://b")]
    )
    assert list(df.loc[df["step"] == 0, "img"]) == ["ref://a"]
    assert df.loc[df["step"] == 0, "img__tags"].iloc[0] == {"split": "train"}
    assert df.loc[df["step"] == 1, "img__tags"].iloc[0] == {}


def test_build_artifacts_dataframe_empty() -> None:
    assert build_artifacts_dataframe([]).empty


@pytest.mark.parametrize(
    ("metric_columns", "artifact_keys", "expected"),
    [
        (["run_id", "index", "loss"], [], {"loss": ColumnKind.METRIC}),
        (["loss"], ["image"], {"loss": ColumnKind.METRIC, "image": ColumnKind.ARTIFACT}),
        (["shared"], ["shared"], {"shared": ColumnKind.ARTIFACT}),
    ],
)
def test_infer_column_kinds(
    metric_columns: list[str], artifact_keys: list[str], expected: dict[str, ColumnKind]
) -> None:
    assert infer_column_kinds(metric_columns, artifact_keys) == expected


def test_group_columns_by_kind_round_trips_plain_strings() -> None:
    grouped = group_columns_by_kind({"loss": "metric", "image": "artifact", "acc": "metric"})
    assert grouped == {ColumnKind.METRIC: ["loss", "acc"], ColumnKind.ARTIFACT: ["image"]}


@pytest.mark.parametrize(
    ("metrics_empty", "artifacts_empty"),
    [(False, False), (True, False), (False, True)],
)
def test_merge_metrics_and_artifacts(*, metrics_empty: bool, artifacts_empty: bool) -> None:
    metrics_df = (
        pd.DataFrame() if metrics_empty else pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.1]})
    )
    artifacts_df = (
        pd.DataFrame() if artifacts_empty else pd.DataFrame({"run_id": [1], "step": [0], "img": ["ref://a"]})
    )
    merged = merge_metrics_and_artifacts(metrics_df, artifacts_df)

    if metrics_empty and artifacts_empty:
        assert merged.empty
    elif metrics_empty:
        assert list(merged.columns) == ["run_id", "step", "img"]
    elif artifacts_empty:
        assert list(merged.columns) == ["run_id", "step", "loss"]
    else:
        assert set(merged.columns) == {"run_id", "step", "loss", "img"}


@pytest.mark.parametrize(
    ("excluded", "expected_runs"),
    [
        ([], [1, 2]),
        ([1], [2]),
        ([1, 2], []),
    ],
)
def test_filter_excluded_runs(excluded: list[int], expected_runs: list[int]) -> None:
    df = pd.DataFrame({"run_id": [1, 2], "value": [10, 20]})
    filtered = filter_excluded_runs(df, {"excluded_runs": excluded})
    assert sorted(filtered["run_id"]) == expected_runs


def test_filter_excluded_runs_empty_df_is_noop() -> None:
    df = pd.DataFrame()
    assert filter_excluded_runs(df, {"excluded_runs": [1]}).empty
