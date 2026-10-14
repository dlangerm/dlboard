"""Building a panel's dataframe: artifacts, hyperparameters and metrics merged into one wide frame."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest

from dlboard import models
from dlboard.models import (
    HPARAM_COLUMN_PREFIX,
    Artifact,
    HyperParams,
    NewHyperParams,
    ValidJsonTypes,
    artifact_column,
    artifact_fname_column,
    artifact_keys_in,
    artifact_tags_column,
)
from dlboard.models._view import ChartInstance, PanelInstance
from dlboard.plugins.charts.file_list import FileListChart
from dlboard.plugins.charts.image_series import ImageChart
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.serve._backend._artifact_download import artifact_url
from dlboard.serve._pages._experiment import _panel_data as panel_data

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


LineChart.register(allow_override=True)
FileListChart.register(allow_override=True)
ImageChart.register(allow_override=True)


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
    df = panel_data.build_artifacts_dataframe([first, second])
    assert list(df.loc[df["step"] == 0, artifact_column("img")]) == [artifact_url(first)]
    assert list(df.loc[df["step"] == 1, artifact_column("img")]) == [artifact_url(second)]
    assert df.loc[df["step"] == 0, artifact_tags_column("img")].iloc[0] == {"split": "train"}
    assert df.loc[df["step"] == 1, artifact_tags_column("img")].iloc[0] == {}


def test_build_artifacts_dataframe_empty() -> None:
    assert panel_data.build_artifacts_dataframe([]).empty


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
    merged = panel_data.merge_metrics_and_artifacts(metrics_df, artifacts_df)

    if metrics_empty and artifacts_empty:
        assert merged.empty
    elif metrics_empty:
        assert list(merged.columns) == ["run_id", "step", "img"]
    elif artifacts_empty:
        assert list(merged.columns) == ["run_id", "step", "loss"]
    else:
        assert set(merged.columns) == {"run_id", "step", "loss", "img"}


def test_build_hyperparams_dataframe_prefixes_columns() -> None:
    df = panel_data.build_hyperparams_dataframe([_hparams(1, run_id=1, lr=0.1, batch_size=32)])
    assert set(df.columns) == {"run_id", f"{HPARAM_COLUMN_PREFIX}lr", f"{HPARAM_COLUMN_PREFIX}batch_size"}
    assert df.loc[df["run_id"] == 1, f"{HPARAM_COLUMN_PREFIX}lr"].iloc[0] == 0.1


def test_build_hyperparams_dataframe_filters_by_keys() -> None:
    df = panel_data.build_hyperparams_dataframe([_hparams(1, run_id=1, lr=0.1, batch_size=32)], keys={"lr"})
    assert set(df.columns) == {"run_id", f"{HPARAM_COLUMN_PREFIX}lr"}


def test_build_hyperparams_dataframe_empty() -> None:
    assert panel_data.build_hyperparams_dataframe([]).empty


def test_merge_hyperparams_broadcasts_per_run_values_onto_every_step_row() -> None:
    df = pd.DataFrame({"run_id": [1, 1, 2], "step": [0, 1, 0], "loss": [0.5, 0.4, 0.9]})
    hparams_df = panel_data.build_hyperparams_dataframe(
        [_hparams(1, run_id=1, lr=0.1), _hparams(2, run_id=2, lr=0.2)]
    )

    merged = panel_data.merge_hyperparams(df, hparams_df)

    assert list(merged.loc[merged["run_id"] == 1, f"{HPARAM_COLUMN_PREFIX}lr"]) == [0.1, 0.1]
    assert merged.loc[merged["run_id"] == 2, f"{HPARAM_COLUMN_PREFIX}lr"].iloc[0] == 0.2


def test_merge_hyperparams_empty_hparams_is_noop() -> None:
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})
    merged = panel_data.merge_hyperparams(df, pd.DataFrame())
    pd.testing.assert_frame_equal(merged, df)


def test_merge_hyperparams_empty_df_returns_hparams_df() -> None:
    hparams_df = panel_data.build_hyperparams_dataframe([_hparams(1, run_id=1, lr=0.1)])
    merged = panel_data.merge_hyperparams(pd.DataFrame(), hparams_df)
    pd.testing.assert_frame_equal(merged, hparams_df)


@pytest.mark.usefixtures("_plain_artifact_urls")
def test_build_artifacts_dataframe_carries_each_artifacts_file_name() -> None:
    """The URL has no extension, so a chart tells an image from a file by this."""
    artifact = _artifact(1, 0, "img", "ref://a").model_copy(update={"id": 1, "fname": "a.png"})
    df = panel_data.build_artifacts_dataframe([artifact])
    assert df[artifact_fname_column("img")].iloc[0] == "a.png"


def _log_a_metric(store: SQLLiteStore, experiment_id: int, run_id: int, key: str, step: int = 0) -> None:
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={key: 1.0}, step=step, experiment_id=experiment_id, run_id=run_id, timestamp_utc=_TS
            )
        ]
    )


@pytest.mark.usefixtures("_plain_artifact_urls")
def test_a_metric_and_an_artifact_sharing_a_key_both_render(store: SQLLiteStore, experiment_id: int) -> None:
    """They used to be outer-merged into `img_x`/`img_y`, so a chart of either `img` raised `KeyError`."""
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    _log_a_metric(store, experiment_id, run.id, "img")
    store.log_artifact_refs(
        [
            models.Artifact(
                key="img", fname="i.png", run_id=run.id, experiment_id=experiment_id, step=0, ref="r://a"
            )
        ]
    )
    panel = PanelInstance[pd.DataFrame, object](
        name="p",
        charts=[
            ChartInstance[pd.DataFrame, object](
                chart_type="line", parameters={"column": "img", "x_axis": "step"}
            ),
            ChartInstance[pd.DataFrame, object](chart_type="image", parameters={"key": "img"}),
        ],
    )

    df = panel_data.fetch_panel_dataframe(store, experiment_id, panel)

    for chart in panel.charts:
        chart.render(df)


@pytest.mark.usefixtures("_plain_artifact_urls")
def test_a_file_list_of_a_key_prefix_fetches_every_key_under_it_including_later_ones(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))

    def log_file(key: str, step: int) -> None:
        store.log_artifact_refs(
            [
                models.Artifact(
                    key=key,
                    fname=key,
                    run_id=run.id,
                    experiment_id=experiment_id,
                    step=step,
                    ref=f"r://{key}",
                    tags={models.FILE_KIND_TAG: "checkpoint"},
                )
            ]
        )

    panel = PanelInstance[pd.DataFrame, object](
        name="p",
        charts=[ChartInstance[pd.DataFrame, object](chart_type="files", parameters={"key_prefix": "ckpt/"})],
    )
    log_file("ckpt/a", 0)
    log_file("elsewhere/b", 0)

    first = panel_data.fetch_panel_dataframe(store, experiment_id, panel)
    log_file("ckpt/c", 1)
    later = panel_data.fetch_panel_dataframe(store, experiment_id, panel)

    def files(df: pd.DataFrame) -> set[str]:
        return set(artifact_keys_in(df.columns, "", prefix=True))

    assert files(first) == {"ckpt/a"}
    assert files(later) == {"ckpt/a", "ckpt/c"}


@pytest.mark.parametrize(
    ("new_row", "redraws"),
    [
        pytest.param({"loss": 0.5, "acc": None}, True, id="a-new-point-for-the-chart"),
        pytest.param({"loss": None, "acc": 0.9}, False, id="a-new-point-for-another-metric-only"),
    ],
)
def test_a_chart_only_redraws_for_rows_where_it_has_data_of_its_own(
    new_row: dict[str, float | None], redraws: bool
) -> None:
    """The panel's dataframe is wide, so another metric logging a step adds a (blank) row for this chart."""
    chart = ChartInstance[pd.DataFrame, object](
        chart_type="line", parameters={"column": "loss", "x_axis": "step"}
    )
    before = pd.DataFrame({"run_id": [1], "step": [0], "loss": [1.0], "acc": [0.8]})
    after = pd.concat([before, pd.DataFrame({"run_id": [1], "step": [1], **new_row})], ignore_index=True)

    changed = panel_data.chart_data_fingerprint(chart, before) != panel_data.chart_data_fingerprint(
        chart, after
    )

    assert changed is redraws


_LINE = ChartInstance[Any, Any](chart_type="line", parameters={"column": "loss", "x_axis": "step"})
_FILES = ChartInstance[Any, Any](chart_type="files", parameters={"key": "ckpt"})
_TABLE_WITH_ARTIFACTS = ChartInstance[Any, Any](chart_type="table", parameters={"artifacts": ["img"]})


@pytest.mark.parametrize(
    ("charts", "splits"),
    [
        pytest.param([_LINE, _FILES], True, id="a-metric-chart-and-an-artifact-chart"),
        pytest.param([_FILES], False, id="artifacts-only"),
        pytest.param([_TABLE_WITH_ARTIFACTS], False, id="one-chart-wanting-both"),
        pytest.param([_LINE, _TABLE_WITH_ARTIFACTS], False, id="the-artifacts-come-with-metrics-anyway"),
    ],
)
def test_a_panel_is_split_only_when_different_charts_want_metrics_and_artifacts(
    charts: list[ChartInstance[Any, Any]], *, splits: bool
) -> None:
    TableChart.register(allow_override=True)
    panel = PanelInstance[Any, Any](name="p", charts=charts)
    assert panel_data._splits_metric_and_artifact_charts(panel) is splits
