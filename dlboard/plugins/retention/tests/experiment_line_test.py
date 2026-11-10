"""An experiment's header says what retention keeps of it, its own rule and its project's, and links to where both are set."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pendulum
import pytest

from dlboard import models
from dlboard.conftest import dispose_stores, find_all_props
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dlboard.plugins import retention as retention_plugin
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.plugins.retention._rules import (
    RetentionKey,
    RetentionRule,
    RetentionSetting,
    RuleVersion,
    RunAge,
    RunCount,
)
from dlboard.plugins.retention._section import SECTION_ID
from dlboard.serve import app as build_app
from dlboard.serve import get_system_data_store
from dlboard.serve._backend import _auth
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment._header import experiment_header

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from dash import Dash


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Dash]:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    built = build_app([sqlite, filesystem, retention_plugin, *LOCAL_AUTH, *BUILTIN_BACKEND])
    yield built
    dispose_stores(built)


def _in_effect(rule: RetentionRule) -> dict[str, Any]:
    now = pendulum.now("UTC")
    return RetentionSetting(active=RuleVersion(rule=rule, set_at=now, effective_at=now)).dump()


@pytest.mark.parametrize(
    ("project_rule", "own_rule", "line"),
    [
        pytest.param(None, None, "Follows the project · Project: Keeps every run", id="no-rules"),
        pytest.param(
            RunCount(max_runs=5),
            None,
            "Follows the project · Project: Keeps the newest 5 runs of each experiment",
            id="the-project's",
        ),
        pytest.param(
            RunCount(max_runs=5),
            RunAge(max_age_days=7),
            "Keeps runs from the last 7 days · Project: Keeps the newest 5 runs of each experiment",
            id="its-own-and-the-project's",
        ),
    ],
)
def test_an_experiment_header_says_its_rule_and_its_projects_and_links_to_the_project_section(
    app: Dash, project_rule: RetentionRule | None, own_rule: RetentionRule | None, line: str
) -> None:
    store = get_system_data_store(app)
    owner = store.get_or_create_user(models.Principal.unverified("owner"))
    project = store.create_project(models.NewProject(name="p", description=""))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    for experiment_id, rule in ((None, project_rule), (experiment.id, own_rule)):
        if rule is not None:
            store.put_scoped_setting(RetentionKey.RULE, project.id, experiment_id, _in_effect(rule), owner)

    with app.server.test_request_context():
        _auth.bind_current_user(owner)
        page = repo.load_page(store, repo.ExperimentRef(experiment.id, None))
        header = experiment_header(store, experiment, page, None)

    assert [p["children"] for p in find_all_props(header, lambda p: p.get("children") == line)] == [line]
    [link] = find_all_props(header, lambda p: p.get("href", "").endswith(f"#{SECTION_ID}"))
    assert link["href"] == f"/project/{project.id}#{SECTION_ID}"
