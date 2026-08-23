# pyright: reportPrivateUsage=false
"""End-to-end tests against a real (temp-file) sqlite-backed data store.

Covers the sql generation/escaping/decoding in `_sql.py` and the CRUD flows in
`SQLStoreBase` together, using the real sqlite backend rather than mocks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from dltrack import models
from dltrack.models._view import PanelInstance
from dltrack.plugins.data_stores.sqlite import SQLLiteStore
from dltrack.plugins.pages.simple_experiment_page import BasicExperimentPage

if TYPE_CHECKING:
    from pathlib import Path

_TS = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> SQLLiteStore:
    return SQLLiteStore(tmp_path / "test.sqlite")


@pytest.fixture
def experiment_id(store: SQLLiteStore) -> int:
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    return experiment.id


def test_project_and_experiment_are_persisted(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    assert store.get_project(project.id) == project

    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    assert store.get_experiment(experiment.id) == experiment
    assert list(store.get_experiments(project.id)) == [experiment]
    assert store.get_experiment(experiment.id + 1000) is None


def test_create_experiment_stores_name_and_description(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(
        models.NewExperiment(project_id=project.id, name="my-exp", description="does a thing")
    )

    assert experiment.name == "my-exp"
    assert experiment.description == "does a thing"
    assert store.get_experiment(experiment.id) == experiment


def test_create_experiment_defaults_name_and_description_to_empty(store: SQLLiteStore) -> None:
    """The CLI logger creates experiments without a name/description; both must stay optional."""
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))

    assert experiment.name == ""
    assert experiment.description == ""


def test_update_project_persists_description_change(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="old"))

    updated = store.update_project(project.model_copy(update={"description": "new"}))

    assert updated.id == project.id
    assert updated.description == "new"
    assert store.get_project(project.id).description == "new"


def test_update_experiment_persists_name_and_description_change(
    store: SQLLiteStore, experiment_id: int
) -> None:
    experiment = store.get_experiment(experiment_id)
    assert experiment is not None

    updated = store.update_experiment(
        experiment.model_copy(update={"name": "renamed", "description": "new description"})
    )

    assert updated.id == experiment_id
    assert updated.name == "renamed"
    assert updated.description == "new description"
    refetched = store.get_experiment(experiment_id)
    assert refetched is not None
    assert (refetched.name, refetched.description) == ("renamed", "new description")


def test_log_hyperparams_skips_duplicate_run(store: SQLLiteStore, experiment_id: int) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    first = store.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment_id, {"lr": 0.1}))
    second = store.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment_id, {"lr": 0.2}))

    assert second.id == first.id
    assert [h.id for h in store.fetch_hyperparams(experiment_id)] == [first.id]


def test_log_and_fetch_metrics_round_trips_and_groups_by_step(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"loss": 0.5}, step=0, experiment_id=experiment_id, run_id=run.id, timestamp_utc=_TS
            ),
            models.LoggedMetrics(
                metrics={"loss": 0.4, "acc": 0.9},
                step=1,
                experiment_id=experiment_id,
                run_id=run.id,
                timestamp_utc=_TS,
            ),
        ]
    )

    fetched = list(store.fetch_metrics(experiment_id))
    assert [m.step for m in fetched] == [0, 1]
    assert fetched[0].metrics == {"loss": 0.5}
    assert fetched[1].metrics == {"loss": 0.4, "acc": 0.9}


def test_log_and_fetch_artifacts_decodes_tags(store: SQLLiteStore, experiment_id: int) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_artifact_refs(
        [
            models.Artifact(
                key="img",
                fname="img.png",
                run_id=run.id,
                experiment_id=experiment_id,
                step=0,
                ref="ref://a",
                tags={"split": "train"},
            ),
            models.Artifact(
                key="other",
                fname="o.bin",
                run_id=run.id,
                experiment_id=experiment_id,
                step=0,
                ref="ref://b",
            ),
        ]
    )

    fetched = list(store.fetch_artifacts(experiment_id=experiment_id, keys={"img"}))
    assert len(fetched) == 1
    assert fetched[0].tags == {"split": "train"}
    assert fetched[0].ref == "ref://a"


@pytest.mark.parametrize(
    ("run_id", "experiment_id_", "project_id"),
    [
        (None, None, None),
        (1, 1, None),
    ],
)
def test_get_or_create_page_requires_exactly_one_id(
    store: SQLLiteStore, run_id: int | None, experiment_id_: int | None, project_id: int | None
) -> None:
    with pytest.raises(ValueError, match="Exactly one"):
        store.get_or_create_page(
            BasicExperimentPage, run_id=run_id, experiment_id=experiment_id_, project_id=project_id
        )


def test_get_or_create_page_is_idempotent_and_updatable(store: SQLLiteStore, experiment_id: int) -> None:
    page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    again = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    assert again.id == page.id

    updated = page.model_copy(
        update={
            "panels": [PanelInstance(name="metrics")],
            "page_settings": {"open_panel": ["metrics"]},
        }
    )
    stored = store.update_page(updated)

    reloaded = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    assert reloaded.id == page.id
    assert [p.name for p in reloaded.panels] == ["metrics"]
    assert reloaded.page_settings == {"open_panel": ["metrics"]}
    assert stored.id == page.id
