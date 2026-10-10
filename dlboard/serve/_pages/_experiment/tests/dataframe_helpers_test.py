# pyright: reportPrivateUsage=false
"""Tests for the pure dataframe-building helpers used by experiment pages."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from dlboard.models import (
    FILE_KIND_TAG,
    Artifact,
    HyperParams,
    LoggedMetrics,
    NewHyperParams,
    NewRun,
    ValidJsonTypes,
)
from dlboard.models._view import ColumnKind
from dlboard.plugins.charts._table_style import HPARAM_COLUMN_PREFIX, artifact_column, artifact_tags_column
from dlboard.serve._backend._artifact_download import artifact_url
from dlboard.serve._pages._experiment import _dataframe_helpers as dfh

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _artifact(run_id: int, step: int, key: str, ref: str, tags: dict[str, str] | None = None) -> Artifact:
    return Artifact(
        id=1, key=key, fname=f"{key}.bin", run_id=run_id, experiment_id=1, step=step, ref=ref, tags=tags or {}
    )


def _hparams(hparam_id: int, run_id: int, **values: ValidJsonTypes) -> HyperParams:
    return HyperParams(
        id=hparam_id,
        run_id=run_id,
        experiment_id=1,
        raw_hparams=NewHyperParams.from_raw(run_id, 1, values).raw_hparams,
    )


@pytest.mark.usefixtures("_plain_artifact_urls")
def test_build_artifacts_dataframe_pivots_url_and_tags() -> None:
    """The pivoted column holds a fetchable `/artifact/<id>` URL, not the artifact's own `ref`."""
    first = _artifact(1, 0, "img", "ref://a", tags={"split": "train"}).model_copy(update={"id": 1})
    second = _artifact(1, 1, "img", "ref://b").model_copy(update={"id": 2})
    df = dfh.build_artifacts_dataframe([first, second])
    assert list(df.loc[df["step"] == 0, artifact_column("img")]) == [artifact_url(first)]
    assert list(df.loc[df["step"] == 1, artifact_column("img")]) == [artifact_url(second)]
    assert df.loc[df["step"] == 0, artifact_tags_column("img")].iloc[0] == {"split": "train"}
    assert df.loc[df["step"] == 1, artifact_tags_column("img")].iloc[0] == {}


def test_build_artifacts_dataframe_empty() -> None:
    assert dfh.build_artifacts_dataframe([]).empty


_CATALOG = dfh.ColumnCatalog(metrics=("loss", "shared"), artifacts=("shared",), hparams=("lr",))


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (ColumnKind.METRIC, ["loss", "shared", "step", "timestamp_utc"]),
        # A metric and an artifact sharing a name are each offered under their own kind -- the old
        # `{name: kind}` dict could only hold one of them.
        (ColumnKind.ARTIFACT, ["shared"]),
        (ColumnKind.HPARAM, ["lr"]),
        (ColumnKind.GROUPING, ["loss", "lr", "run_id", "shared", "step", "timestamp_utc"]),
    ],
)
def test_column_catalog_options_by_kind(kind: ColumnKind, expected: list[str]) -> None:
    assert _CATALOG.options(kind) == expected


def test_column_catalog_offers_no_metric_axes_without_metrics() -> None:
    assert dfh.ColumnCatalog(artifacts=("img",)).options(ColumnKind.METRIC) == []


def test_column_catalog_load_summarizes_keys_without_their_values(
    store: SQLLiteStore, experiment_id: int
) -> None:
    runs = [store.create_run(NewRun(experiment_id=experiment_id)) for _ in range(2)]
    steps: list[dict[str, float | None]] = [{"loss": 0.5}, {"loss": 0.4, "final_acc": 0.9}]
    for run in runs:
        store.log_metrics(
            LoggedMetrics(
                metrics=metrics, step=step, experiment_id=experiment_id, run_id=run.id, timestamp_utc=_TS
            )
            for step, metrics in enumerate(steps)
        )
        store.log_hyperparams(NewHyperParams.from_raw(run.id, experiment_id, {"lr": 0.1}))
    store.log_artifact_refs(
        [
            Artifact(
                key="img", fname="i.png", run_id=runs[0].id, experiment_id=experiment_id, step=0, ref="r://a"
            ),
            Artifact(
                key="ckpt",
                fname="last.ckpt",
                run_id=runs[0].id,
                experiment_id=experiment_id,
                step=0,
                ref="r://b",
                tags={FILE_KIND_TAG: "checkpoint"},
            ),
            Artifact(
                key="grads",
                fname="grads.npy",
                run_id=runs[0].id,
                experiment_id=experiment_id,
                step=0,
                ref="r://c",
            ),
        ]
    )

    catalog = dfh.ColumnCatalog.load(store, experiment_id)

    assert catalog == dfh.ColumnCatalog(
        metrics=("final_acc", "loss"),
        single_value_metrics=frozenset({"final_acc"}),
        artifacts=("ckpt", "grads", "img"),
        artifact_chart_types={"ckpt": "files", "img": "image"},  # nothing displays the grads
        hparams=("lr",),
    )


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
