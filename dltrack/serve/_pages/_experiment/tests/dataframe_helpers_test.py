# pyright: reportPrivateUsage=false
"""Tests for the pure dataframe-building helpers used by experiment pages."""

from __future__ import annotations

import pandas as pd
import pytest

from dltrack.models import Artifact, HyperParams, NewHyperParams, ValidJsonTypes
from dltrack.models._view import ColumnKind
from dltrack.plugins.charts._table_style import HPARAM_COLUMN_PREFIX
from dltrack.serve._pages._experiment import _dataframe_helpers as dfh


def _artifact(run_id: int, step: int, key: str, ref: str, tags: dict[str, str] | None = None) -> Artifact:
    return Artifact(
        key=key, fname=f"{key}.bin", run_id=run_id, experiment_id=1, step=step, ref=ref, tags=tags or {}
    )


def _hparams(hparam_id: int, run_id: int, **values: ValidJsonTypes) -> HyperParams:
    return HyperParams(
        id=hparam_id,
        run_id=run_id,
        experiment_id=1,
        raw_hparams=NewHyperParams.from_raw(run_id, 1, values).raw_hparams,
    )


def test_build_artifacts_dataframe_pivots_ref_and_tags() -> None:
    df = dfh.build_artifacts_dataframe(
        [_artifact(1, 0, "img", "ref://a", tags={"split": "train"}), _artifact(1, 1, "img", "ref://b")]
    )
    assert list(df.loc[df["step"] == 0, "img"]) == ["ref://a"]
    assert df.loc[df["step"] == 0, "img__tags"].iloc[0] == {"split": "train"}
    assert df.loc[df["step"] == 1, "img__tags"].iloc[0] == {}


def test_build_artifacts_dataframe_empty() -> None:
    assert dfh.build_artifacts_dataframe([]).empty


@pytest.mark.parametrize(
    ("metric_columns", "artifact_keys", "hparam_keys", "expected"),
    [
        (["run_id", "loss"], [], [], {"loss": ColumnKind.METRIC}),
        (["loss"], ["image"], [], {"loss": ColumnKind.METRIC, "image": ColumnKind.ARTIFACT}),
        (["shared"], ["shared"], [], {"shared": ColumnKind.ARTIFACT}),
        (
            ["loss"],
            ["image"],
            ["lr"],
            {"loss": ColumnKind.METRIC, "image": ColumnKind.ARTIFACT, "lr": ColumnKind.HPARAM},
        ),
    ],
)
def test_infer_column_kinds(
    metric_columns: list[str],
    artifact_keys: list[str],
    hparam_keys: list[str],
    expected: dict[str, ColumnKind],
) -> None:
    assert dfh.infer_column_kinds(metric_columns, artifact_keys, hparam_keys) == expected


def test_group_columns_by_kind_round_trips_plain_strings() -> None:
    grouped = dfh.group_columns_by_kind(
        {"loss": ColumnKind.METRIC, "image": ColumnKind.ARTIFACT, "acc": ColumnKind.METRIC}
    )
    assert grouped == {
        ColumnKind.METRIC: ["loss", "acc"],
        ColumnKind.ARTIFACT: ["image"],
        ColumnKind.GROUPING: ["acc", "loss", "run_id"],
    }


def test_group_columns_by_kind_grouping_bucket_includes_hparams_and_run_id() -> None:
    grouped = dfh.group_columns_by_kind({"loss": ColumnKind.METRIC, "lr": ColumnKind.HPARAM})
    assert grouped[ColumnKind.GROUPING] == ["loss", "lr", "run_id"]


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
    merged = dfh.merge_metrics_and_artifacts(metrics_df, artifacts_df)

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
    filtered = dfh.filter_excluded_runs(df, {"excluded_runs": excluded})
    assert sorted(filtered["run_id"]) == expected_runs


def test_filter_excluded_runs_empty_df_is_noop() -> None:
    df = pd.DataFrame()
    assert dfh.filter_excluded_runs(df, {"excluded_runs": [1]}).empty


def test_build_hyperparams_dataframe_prefixes_columns() -> None:
    df = dfh.build_hyperparams_dataframe([_hparams(1, run_id=1, lr=0.1, batch_size=32)])
    assert set(df.columns) == {"run_id", f"{HPARAM_COLUMN_PREFIX}lr", f"{HPARAM_COLUMN_PREFIX}batch_size"}
    assert df.loc[df["run_id"] == 1, f"{HPARAM_COLUMN_PREFIX}lr"].iloc[0] == 0.1


def test_build_hyperparams_dataframe_filters_by_keys() -> None:
    df = dfh.build_hyperparams_dataframe([_hparams(1, run_id=1, lr=0.1, batch_size=32)], keys={"lr"})
    assert set(df.columns) == {"run_id", f"{HPARAM_COLUMN_PREFIX}lr"}


def test_build_hyperparams_dataframe_empty() -> None:
    assert dfh.build_hyperparams_dataframe([]).empty


def test_merge_hyperparams_broadcasts_per_run_values_onto_every_step_row() -> None:
    df = pd.DataFrame({"run_id": [1, 1, 2], "step": [0, 1, 0], "loss": [0.5, 0.4, 0.9]})
    hparams_df = dfh.build_hyperparams_dataframe(
        [_hparams(1, run_id=1, lr=0.1), _hparams(2, run_id=2, lr=0.2)]
    )

    merged = dfh.merge_hyperparams(df, hparams_df)

    assert list(merged.loc[merged["run_id"] == 1, f"{HPARAM_COLUMN_PREFIX}lr"]) == [0.1, 0.1]
    assert merged.loc[merged["run_id"] == 2, f"{HPARAM_COLUMN_PREFIX}lr"].iloc[0] == 0.2


def test_merge_hyperparams_empty_hparams_is_noop() -> None:
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})
    merged = dfh.merge_hyperparams(df, pd.DataFrame())
    pd.testing.assert_frame_equal(merged, df)


def test_merge_hyperparams_empty_df_returns_hparams_df() -> None:
    hparams_df = dfh.build_hyperparams_dataframe([_hparams(1, run_id=1, lr=0.1)])
    merged = dfh.merge_hyperparams(pd.DataFrame(), hparams_df)
    pd.testing.assert_frame_equal(merged, hparams_df)


def test_single_value_metric_columns_excludes_metrics_logged_at_multiple_steps() -> None:
    df = pd.DataFrame(
        {
            "run_id": [1, 1, 2, 2],
            "step": [0, 1, 0, 1],
            "final_accuracy": [None, 0.9, None, 0.8],
            "loss": [0.5, 0.4, 0.9, 0.7],
        }
    )
    assert dfh.single_value_metric_columns(df, ["final_accuracy", "loss"]) == {"final_accuracy"}


def test_single_value_metric_columns_empty_df() -> None:
    assert dfh.single_value_metric_columns(pd.DataFrame(), ["loss"]) == set()


def test_single_value_metric_columns_ignores_unknown_columns() -> None:
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})
    assert dfh.single_value_metric_columns(df, ["loss", "missing"]) == {"loss"}
