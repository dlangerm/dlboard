"""The catalog of keys an experiment's charts can be configured with."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum
import pytest

from dlboard.models import (
    FILE_KIND_TAG,
    LAYER_KIND_TAG,
    Artifact,
    Histogram,
    LoggedMetrics,
    NewHyperParams,
    NewRun,
    overlay_key,
)
from dlboard.models._view import ColumnKind
from dlboard.serve._pages._experiment._column_catalog import ColumnCatalog

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

_TS = pendulum.datetime(2026, 1, 1, tz="UTC")

_CATALOG = ColumnCatalog(
    metrics=("loss", "shared"),
    artifacts=("shared",),
    layers=("mask",),
    histograms=("grad",),
    hparams=("lr",),
)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (ColumnKind.METRIC, ["loss", "shared", "step", "timestamp_utc"]),
        # A metric and an artifact sharing a name are each offered under their own kind -- the old
        # `{name: kind}` dict could only hold one of them.
        (ColumnKind.ARTIFACT, ["shared"]),
        (ColumnKind.LAYER, ["mask"]),
        (ColumnKind.HISTOGRAM, ["grad"]),
        (ColumnKind.HPARAM, ["lr"]),
        (ColumnKind.GROUPING, ["loss", "lr", "run_id", "shared", "step", "timestamp_utc"]),
    ],
)
def test_column_catalog_options_by_kind(kind: ColumnKind, expected: list[str]) -> None:
    assert _CATALOG.options(kind) == expected


def test_an_overlay_is_no_choice_of_artifact_when_the_image_it_goes_over_is_logged() -> None:
    drawn = overlay_key("samples/a", "truth")
    orphan = overlay_key("samples/b")  # no "samples/b" is logged
    catalog = ColumnCatalog(artifacts=("samples/a", drawn, "samples/c", orphan))

    # The image's chart draws `drawn`; the orphan has no image to draw it, so it can only be charted as itself.
    assert catalog.options(ColumnKind.ARTIFACT) == ["samples/a", "samples/c", orphan]


def test_column_catalog_offers_no_metric_axes_without_metrics() -> None:
    assert ColumnCatalog(artifacts=("img",)).options(ColumnKind.METRIC) == []


def test_a_histogram_alone_is_something_to_chart() -> None:
    assert ColumnCatalog(histograms=("grad",)).has_chartable_keys


def test_column_catalog_load_summarizes_keys_without_their_values(
    store: SQLLiteStore, experiment_id: int
) -> None:
    runs = [store.create_run(NewRun(experiment_id=experiment_id)) for _ in range(2)]
    steps: list[dict[str, float | None]] = [{"loss": 0.5}, {"loss": 0.4, "final_acc": 0.9}]
    for run in runs:
        store.log_metrics(
            LoggedMetrics(
                metrics=metrics,
                histograms={"grad": Histogram(edges=[0.0, 1.0], counts=[step + 1])},
                step=step,
                experiment_id=experiment_id,
                run_id=run.id,
                timestamp_utc=_TS,
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
                key="boxes",
                fname="boxes.json",
                run_id=runs[0].id,
                experiment_id=experiment_id,
                step=0,
                ref="r://d",
                tags={LAYER_KIND_TAG: "shapes"},
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

    catalog = ColumnCatalog.load(store, experiment_id)

    assert catalog == ColumnCatalog(
        metrics=("final_acc", "loss"),
        single_value_metrics=frozenset({"final_acc"}),
        artifacts=("boxes", "ckpt", "grads", "img"),
        artifact_chart_types={"ckpt": "files", "img": "image"},  # nothing displays the grads
        layers=("boxes", "img"),  # the shapes say so; the image is drawn as it is; no one draws a checkpoint
        histograms=("grad",),
        hparams=("lr",),
    )
