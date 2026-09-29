# pyright: reportPrivateUsage=false
"""
A sqlite database written by the old raw-SQL store must open, read, and write unchanged.

`legacy_schema.sql` is a real `sqlite3 .dump` of one, frozen: text timestamps in both `Z` and
`+00:00` spellings, JSON in TEXT columns, bools as integers, and the old column-list index names.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from dltrack import models
from dltrack.plugins.data_stores.sqlite import SQLLiteStore
from dltrack.serve._pages._experiment._experiment_page_state import BasicExperimentPage

_CREATED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.fixture
def legacy(tmp_path: Path) -> SQLLiteStore:
    db_path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript((Path(__file__).parent / "legacy_schema.sql").read_text())
    return SQLLiteStore(db_path)


def test_every_legacy_entity_reads_back(legacy: SQLLiteStore) -> None:
    admin, bob = legacy.get_or_create_user("admin"), legacy.get_or_create_user("bob")
    assert (admin.id, admin.scopes, bob.scopes) == (1, [models.Scope.ALL], [])
    assert legacy.get_or_create_user("carol").scopes == [], "the bootstrap grant was already claimed"

    project = legacy.get_project(1)
    assert (project.name, project.created_at) == ("legacy", _CREATED)
    experiment = legacy.get_experiment(1)
    assert experiment is not None
    assert (experiment.source, experiment.revision, experiment.notes_revision) == (
        models.ExperimentSource.PYTORCH_LIGHTNING,
        5,
        1,
    )
    assert experiment.last_activity_at is not None
    assert experiment.last_activity_at.tzinfo is not None
    assert [run.name for run in legacy.get_runs(1)] == ["r0"]

    metrics = legacy.fetch_metrics(1)
    assert list(metrics.step) == [0, 1, 2]
    assert list(metrics.values("loss")) == [1.0, 0.5, pytest.approx(1 / 3)]
    assert math.isnan(metrics.values("acc")[0])
    assert metrics.timestamp_utc[0] == _CREATED
    (hparams,) = legacy.fetch_hyperparams(1)
    assert hparams.hparams_dict == {"lr": 0.1, "opt": "adam", "ema": True}
    (artifact,) = legacy.fetch_artifacts(1)
    assert (artifact.tags, artifact.created_at) == ({"split": "val"}, _CREATED)

    page = legacy.get_or_create_page(BasicExperimentPage, experiment_id=1)
    assert (page.id, [p.name for p in page.panels], page.page_settings) == (
        1,
        ["metrics"],
        {"open_panel": ["metrics"]},
    )
    assert legacy.list_views(1, bob.id) == [models.ViewSummary(id=2, name="bob's view")]
    (comment,) = legacy.list_comments(1)
    assert (comment.body, comment.run_ids) == ("hi", [1])
    (entry,) = legacy.list_audit_log(admin)
    assert (entry.action, entry.entity_type, entry.entity_id) == (
        models.AuditAction.SOFT_DELETE,
        models.EntityType.RUN,
        2,
    )


def test_legacy_rows_restore_and_new_rows_continue_their_ids(legacy: SQLLiteStore) -> None:
    admin = legacy.get_or_create_user("admin")

    legacy.restore_run(2, admin)  # matches the legacy `deleted_at` text exactly
    new_run = legacy.create_run(models.NewRun(experiment_id=1, name="new"))

    names = [run.name for run in legacy.get_runs(1)]
    assert (names[0], set(names[1:])) == ("new", {"doomed", "r0"})  # the two legacy runs tie on `created_at`
    assert new_run.id == 3


def test_legacy_indexes_are_replaced(legacy: SQLLiteStore) -> None:
    with legacy._engine.connect() as conn:
        inspector = sa.inspect(conn)
        names = {
            index["name"]
            for table in ("UnderlyingMetricTableEntry", "Artifact", "Page")
            for index in inspector.get_indexes(table)
        }
    assert names == {
        "idx_metric_lookup",
        "idx_artifact_lookup",
        "idx_Page_shared_run_id",
        "idx_Page_shared_experiment_id",
        "idx_Page_shared_project_id",
    }
