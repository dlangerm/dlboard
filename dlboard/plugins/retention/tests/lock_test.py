"""A project far behind its rule takes no new runs for now: refused over REST, said on its page, and off unless set."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pendulum
import pytest
from pydantic import ValidationError

from dlboard import models
from dlboard.conftest import dispose_stores
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dlboard.plugins import retention as retention_plugin
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.plugins.retention._rules import RetentionKey, RetentionStatus
from dlboard.plugins.retention._section import _status
from dlboard.plugins.retention._settings import RetentionSettings
from dlboard.serve import SQLStoreBase, get_system_data_store
from dlboard.serve import app as build_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from dash import Dash
    from flask.testing import FlaskClient


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Dash]:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    built = build_app([sqlite, filesystem, retention_plugin, *LOCAL_AUTH, *BUILTIN_BACKEND])
    yield built
    dispose_stores(built)


@pytest.mark.parametrize(
    ("lock_after", "behind_hours", "locked"),
    [
        pytest.param(None, 1000, False, id="no-lock-set"),
        pytest.param("48", 100, True, id="behind-for-longer-than-the-lock"),
        pytest.param("48", 30, False, id="not-for-long-enough"),
        pytest.param("48", None, False, id="caught-up"),
    ],
)
def test_a_project_behind_its_rule_for_too_long_takes_no_new_runs_until_it_catches_up(
    app: Dash,
    monkeypatch: pytest.MonkeyPatch,
    lock_after: str | None,
    behind_hours: int | None,
    *,
    locked: bool,
) -> None:
    if lock_after is not None:
        monkeypatch.setenv("DLBOARD_RETENTION_LOCK_AFTER_HOURS", lock_after)
    store = get_system_data_store(app)
    assert isinstance(store, SQLStoreBase)
    admin = store.get_or_create_user(models.Principal.unverified("admin"))
    project = store.create_project(models.NewProject(name="p", description="")).id
    experiment = store.create_experiment(models.NewExperiment(project_id=project)).id
    now = pendulum.now("UTC")
    since = now.subtract(hours=behind_hours) if behind_hours is not None else None
    status = RetentionStatus(checked_at=now, overdue_runs=7 if since else 0, out_of_spec_since=since)
    store.put_scoped_setting(RetentionKey.STATUS, project, None, status.model_dump(mode="json"), admin)

    client: FlaskClient = app.server.test_client()
    reply = client.post("/api/v1/runs", json={"experiment_id": experiment})

    assert reply.status_code == (423 if locked else 200)
    if locked:
        assert "takes no new runs" in reply.get_json()["error"]
    assert len(list(store.get_runs(experiment, run_ids=None))) == (0 if locked else 1)


def test_the_section_says_when_a_project_is_locked_and_the_lock_waits_longer_than_the_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = pendulum.now("UTC")
    status = RetentionStatus(checked_at=now, overdue_runs=3, out_of_spec_since=now.subtract(hours=100))

    def banner() -> Any:  # noqa: ANN401
        return _status(status, now, RetentionSettings())[0]

    assert banner().title == "Behind on retention"
    monkeypatch.setenv("DLBOARD_RETENTION_LOCK_AFTER_HOURS", "48")
    assert (banner().color, banner().title) == ("red", "Locked: behind on retention")

    monkeypatch.setenv("DLBOARD_RETENTION_LOCK_AFTER_HOURS", "12")  # warned at 24: a lock cannot come first
    with pytest.raises(ValidationError, match="warned before it is locked"):
        RetentionSettings()
