# pyright: reportPrivateUsage=false
"""End-to-end tests against a real (temp-file) sqlite-backed data store.

Covers the sql generation/escaping/decoding in `_sql.py` and the CRUD flows in
`SQLStoreBase` together, using the real sqlite backend rather than mocks.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from dltrack import models
from dltrack.models._view import PanelInstance
from dltrack.plugins.data_stores.sqlite import SQLLiteStore
from dltrack.plugins.pages.experiment._experiment_page_state import BasicExperimentPage

if TYPE_CHECKING:
    from pathlib import Path

_TS = datetime(2026, 1, 1, tzinfo=UTC)


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


def test_get_runs_paginates_most_recently_created_first(store: SQLLiteStore, experiment_id: int) -> None:
    runs = [store.create_run(models.NewRun(experiment_id=experiment_id, name=f"run-{i}")) for i in range(3)]

    first_page = list(store.get_runs(experiment_id, limit=2, offset=0))
    second_page = list(store.get_runs(experiment_id, limit=2, offset=2))

    assert [r.id for r in first_page] == [runs[2].id, runs[1].id]
    assert [r.id for r in second_page] == [runs[0].id]


def test_get_runs_defaults_to_a_generous_page_covering_typical_use(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))

    assert list(store.get_runs(experiment_id)) == [run]


def test_get_runs_excludes_deleted_and_other_experiments(store: SQLLiteStore, experiment_id: int) -> None:
    project = store.create_project(models.NewProject(name="other", description="d"))
    other_experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    store.create_run(models.NewRun(experiment_id=other_experiment.id))

    actor = store.get_or_create_user("alice")
    kept = store.create_run(models.NewRun(experiment_id=experiment_id))
    deleted = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.delete_run(deleted.id, actor)

    runs = list(store.get_runs(experiment_id, limit=100, offset=0))

    assert [r.id for r in runs] == [kept.id]


def test_store_init_backfills_a_column_added_to_a_model_since_the_db_was_created(
    tmp_path: Path,
) -> None:
    """
    Reproduces opening a pre-existing db against a model that's grown a field since.

    `CREATE TABLE IF NOT EXISTS` alone is a no-op for a table that already exists, so without
    `_add_missing_columns` this would fail every `Run` read/write with a raw sqlite
    `OperationalError`/`IndexError` instead of picking up the new column.
    """
    db_path = tmp_path / "stale.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE Run (experiment_id INTEGER NOT NULL, created_by INTEGER, "
            "created_at TEXT NOT NULL, id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "deleted_by INTEGER, deleted_at TEXT)"
        )

    store = SQLLiteStore(db_path)
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    run = store.create_run(models.NewRun(experiment_id=experiment.id, name="backfilled"))

    assert run.name == "backfilled"
    assert list(store.get_runs(experiment.id, limit=10, offset=0)) == [run]


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


def test_list_metric_keys_is_distinct_and_sorted_without_fetching_values(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"loss": 0.5, "acc": 0.1},
                step=0,
                experiment_id=experiment_id,
                run_id=run.id,
                timestamp_utc=_TS,
            ),
            models.LoggedMetrics(
                metrics={"loss": 0.4}, step=1, experiment_id=experiment_id, run_id=run.id, timestamp_utc=_TS
            ),
        ]
    )

    assert store.list_metric_keys(experiment_id) == ["acc", "loss"]


def test_list_metric_keys_excludes_deleted_runs_and_other_experiments(
    store: SQLLiteStore, experiment_id: int
) -> None:
    project = store.create_project(models.NewProject(name="other", description="d"))
    other_experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    other_run = store.create_run(models.NewRun(experiment_id=other_experiment.id))
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"other_metric": 1.0},
                step=0,
                experiment_id=other_experiment.id,
                run_id=other_run.id,
                timestamp_utc=_TS,
            )
        ]
    )

    actor = store.get_or_create_user("alice")
    deleted_run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"deleted_run_metric": 1.0},
                step=0,
                experiment_id=experiment_id,
                run_id=deleted_run.id,
                timestamp_utc=_TS,
            )
        ]
    )
    store.delete_run(deleted_run.id, actor)

    assert store.list_metric_keys(experiment_id) == []


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


def test_get_or_create_user_grants_bootstrap_scopes_to_the_first_user_only(store: SQLLiteStore) -> None:
    first = store.get_or_create_user("alice")
    second = store.get_or_create_user("bob")

    assert first.scopes == [models.Scope.ALL]
    assert second.scopes == []


def test_get_or_create_user_is_idempotent(store: SQLLiteStore) -> None:
    first = store.get_or_create_user("alice")
    again = store.get_or_create_user("alice")

    assert again == first
    assert list(store._execute_raw_sql("SELECT count(*) FROM User")) == [(1,)]


def test_get_or_create_project_creates_on_first_call_and_reuses_after(store: SQLLiteStore) -> None:
    first = store.get_or_create_project("p", description="d")
    again = store.get_or_create_project("p", description="ignored on reuse")

    assert again.id == first.id
    assert again.description == "d"
    assert list(store._execute_raw_sql("SELECT count(*) FROM Project")) == [(1,)]


def test_get_or_create_project_stamps_created_by(store: SQLLiteStore) -> None:
    actor = store.get_or_create_user("alice")

    project = store.get_or_create_project("p", created_by=actor.id)

    assert project.created_by == actor.id


def test_get_or_create_experiment_creates_on_first_call_and_reuses_after(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))

    first = store.get_or_create_experiment(project.id)
    again = store.get_or_create_experiment(project.id)

    assert again.id == first.id
    assert again.name == "default"
    assert list(store._execute_raw_sql("SELECT count(*) FROM Experiment")) == [(1,)]


def test_get_or_create_experiment_tags_source_only_on_first_call(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))

    first = store.get_or_create_experiment(project.id, source=models.ExperimentSource.PYTORCH_LIGHTNING)
    again = store.get_or_create_experiment(project.id, source=None)

    assert first.source == models.ExperimentSource.PYTORCH_LIGHTNING
    assert again.id == first.id
    assert again.source == models.ExperimentSource.PYTORCH_LIGHTNING


def test_get_or_create_experiment_is_scoped_to_its_project(store: SQLLiteStore) -> None:
    project_a = store.create_project(models.NewProject(name="a", description="d"))
    project_b = store.create_project(models.NewProject(name="b", description="d"))

    exp_a = store.get_or_create_experiment(project_a.id, name="default")
    exp_b = store.get_or_create_experiment(project_b.id, name="default")

    assert exp_a.id != exp_b.id


def test_update_user_persists_scope_changes(store: SQLLiteStore) -> None:
    user = store.get_or_create_user("alice")
    assert user.scopes == [models.Scope.ALL]  # first user ever, bootstrap admin

    updated = store.update_user(user.model_copy(update={"scopes": [models.Scope.PURGE]}))

    assert updated.scopes == [models.Scope.PURGE]
    assert store.get_or_create_user("alice").scopes == [models.Scope.PURGE]


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
