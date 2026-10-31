"""The catalog of keys an experiment's charts can be configured with."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pendulum
import pytest

from dlboard.models import (
    FILE_KIND_TAG,
    LAYER_KIND_TAG,
    Artifact,
    Histogram,
    LoggedMetrics,
    NewExperiment,
    NewHyperParams,
    NewPage,
    NewRun,
    Principal,
    ValidJsonTypes,
    overlay_key,
)
from dlboard.models._view import ColumnKind
from dlboard.serve._pages._experiment._column_catalog import ColumnCatalog, single_value_metrics
from dlboard.serve._pages._experiment._page_model import BasicExperimentPage

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


def test_only_an_artifact_that_is_not_a_picture_of_its_own_is_offered_as_a_layer_to_add() -> None:
    truth = overlay_key("img", "truth")
    catalog = ColumnCatalog(layers=(truth, "img", "mask", "other-image"), artifacts=("img", truth, "mask"))

    # An overlay is drawn over its picture without being added, and a picture with overlays is what a chart is *of*.
    assert catalog.options(ColumnKind.LAYER) == ["mask", "other-image"]


def test_the_artifact_keys_are_every_key_logged_in_key_order() -> None:
    artifacts = ("img", overlay_key("img", "truth"), overlay_key("img"), "img_$overlayx")
    catalog = ColumnCatalog(artifacts=artifacts)

    assert catalog.artifact_keys() == sorted(artifacts)


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
        artifacts=("boxes", "ckpt", "grads", "img"),
        artifact_chart_types={"ckpt": "files", "img": "image"},  # nothing displays the grads
        layers=("boxes", "img"),  # the shapes say so; the image is drawn as it is; no one draws a checkpoint
        histograms=("grad",),
        hparams=("lr",),
    )


def _logged(
    store: SQLLiteStore, experiment_id: int, metrics: dict[str, list[float]], **hparams: ValidJsonTypes
) -> int:
    """A run of `experiment_id` that logged `metrics` (a value a step) and `hparams`; its id."""
    run = store.create_run(NewRun(experiment_id=experiment_id))
    for key, values in metrics.items():
        store.log_metrics(
            LoggedMetrics(
                metrics={key: value}, step=step, experiment_id=experiment_id, run_id=run.id, timestamp_utc=_TS
            )
            for step, value in enumerate(values)
        )
    store.log_hyperparams(NewHyperParams.from_raw(run.id, experiment_id, hparams))
    return run.id


def test_a_comparisons_catalog_is_the_names_of_its_experiments_and_the_hyperparameters_of_its_members_alone(
    store: SQLLiteStore, experiment_id: int
) -> None:
    experiment = store.get_experiment(experiment_id)
    assert experiment is not None
    other = store.create_experiment(NewExperiment(project_id=experiment.project_id)).id
    member = _logged(store, experiment_id, {"loss": [1.0, 0.5]}, lr=0.1)
    _logged(
        store, experiment_id, {"loss": [1.0]}, momentum=0.9
    )  # in the experiment, but not in the comparison
    other_member = _logged(store, other, {"acc": [0.9]}, wd=0.01)
    comparison = cast(
        "BasicExperimentPage",
        store.create_comparison(
            BasicExperimentPage,
            NewPage[Any, Any](
                project_id=experiment.project_id,
                owner_id=store.get_or_create_user(Principal.unverified("alice")).id,
                name="sweep",
            ),
            frozenset({member, other_member}),
        ),
    )

    catalog = ColumnCatalog.of_page(store, comparison)

    assert catalog.metrics == ("acc", "loss")
    assert catalog.hparams == ("lr", "wd")


def test_an_experiment_pages_catalog_is_its_experiments_as_before(
    store: SQLLiteStore, experiment_id: int
) -> None:
    _logged(store, experiment_id, {"loss": [1.0]}, lr=0.1)
    shared = cast(
        "BasicExperimentPage", store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    )

    assert ColumnCatalog.of_page(store, shared) == ColumnCatalog.load(store, experiment_id)


def test_a_metric_is_single_valued_only_if_no_experiment_logged_it_over_several_steps(
    store: SQLLiteStore, experiment_id: int
) -> None:
    experiment = store.get_experiment(experiment_id)
    assert experiment is not None
    other = store.create_experiment(NewExperiment(project_id=experiment.project_id)).id
    _logged(store, experiment_id, {"final_acc": [0.9], "loss": [1.0]})
    _logged(store, other, {"final_acc": [0.8], "loss": [1.0, 0.5]})

    assert single_value_metrics(store, [experiment_id], ["final_acc", "loss"]) == {"final_acc", "loss"}
    assert single_value_metrics(store, [experiment_id, other], ["final_acc", "loss"]) == {"final_acc"}
    assert single_value_metrics(store, [], ["loss"]) == frozenset()
