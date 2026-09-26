# pyright: reportPrivateUsage=false
"""Shared fixtures and Dash-component helpers for the test suite."""

from __future__ import annotations

import threading
import typing
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

import pytest
from werkzeug.serving import make_server

from dltrack import models
from dltrack.plugins import BUILTIN_BACKEND, LOCAL_AUTH, LOCAL_STORAGE
from dltrack.plugins.data_stores.sqlite import SQLLiteStore
from dltrack.serve import app as build_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class ScreenshotMode(StrEnum):
    """What the `screenshots`-marked tests do with the docs images (`--screenshots`)."""

    CHECK = "check"
    """Fail if a freshly rendered screenshot differs from the committed one."""

    UPDATE = "update"
    """Overwrite the committed screenshots with freshly rendered ones."""


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register `--screenshots`, the switch for the docs-screenshot tests (`docs_screenshots_test.py`)."""
    parser.addoption(
        "--screenshots",
        type=ScreenshotMode,
        choices=list(ScreenshotMode),
        default=None,
        help="Run the docs-screenshot tests: `check` against, or `update`, the images in docs/images.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """
    Keep the docs-screenshot tests out of ordinary runs, and out of everything else in their own.

    Their images depend on the whole database (the home page lists every project), so a run that
    asks for screenshots renders them in a process that ran nothing else.
    """
    screenshots_requested = config.getoption("--screenshots") is not None
    if screenshots_requested:
        deselected = [item for item in items if item.get_closest_marker("screenshots") is None]
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if item not in deselected]
        return
    skip = pytest.mark.skip(reason="docs screenshots are rendered by CI; pass --screenshots=check|update")
    for item in items:
        if item.get_closest_marker("screenshots") is not None:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def screenshot_mode(request: pytest.FixtureRequest) -> ScreenshotMode:
    """The `--screenshots` mode this run was started with."""
    return cast("ScreenshotMode", request.config.getoption("--screenshots"))


@pytest.fixture
def store(tmp_path: Path) -> SQLLiteStore:
    """A throwaway sqlite-backed store for a single test."""
    return SQLLiteStore(tmp_path / "test.sqlite")


@pytest.fixture
def experiment_id(store: SQLLiteStore) -> int:
    """A freshly created project/experiment pair, returning the experiment's id."""
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    return experiment.id


@pytest.fixture
def admin(store: SQLLiteStore) -> models.User:
    """The bootstrap admin -- the first user any fresh store creates gets `Scope.ALL`."""
    return store.get_or_create_user("admin")


class EntityChain(typing.NamedTuple):
    """IDs for a project -> experiment -> run(-> artifact) chain, for tests that need real rows."""

    project_id: int
    experiment_id: int
    run_id: int
    artifact_id: int | None


def create_entity_chain(store: SQLLiteStore, *, artifact: bool = False) -> EntityChain:
    """
    Create a project/experiment/run, and optionally one artifact.

    For tests exercising cascade/soft-delete/audit-log/purge behavior that need a real row at
    each level rather than caring about any of their particular names or content.
    """
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    run = store.create_run(models.NewRun(experiment_id=experiment.id))
    artifact_id = None
    if artifact:
        store.log_artifact_refs(
            [
                models.Artifact(
                    key="img",
                    fname="i.png",
                    run_id=run.id,
                    experiment_id=experiment.id,
                    step=0,
                    ref="ref://a",
                )
            ]
        )
        (logged,) = list(store.fetch_artifacts(experiment_id=experiment.id))
        assert logged.id is not None
        artifact_id = logged.id
    return EntityChain(project.id, experiment.id, run.id, artifact_id)


class BackendServer(typing.NamedTuple):
    """A live dltrack backend's URL, plus a store reading the same sqlite file it writes to."""

    url: str
    store: SQLLiteStore


@pytest.fixture
def backend_server(tmp_path: Path) -> Iterator[BackendServer]:
    """
    A real dltrack backend (storage + REST routes, no charts) on a background thread.

    Deliberately leaves out `BUILTIN_CHARTS`: chart plugins register into a process-global registry
    that rejects a second registration, so only one app per pytest process may include them
    (`browser_test.py`'s). Everything this fixture builds can be built any number of times.
    """
    sqlite_location = tmp_path / "test.sqlite"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("SQLITE_LOCATION", str(sqlite_location))
    monkeypatch.setenv("ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    try:
        app = build_app([*LOCAL_STORAGE, *LOCAL_AUTH, *BUILTIN_BACKEND])
    finally:
        monkeypatch.undo()
    server = make_server("127.0.0.1", 0, app.server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield BackendServer(f"http://127.0.0.1:{server.server_port}", SQLLiteStore(sqlite_location))
    finally:
        server.shutdown()
        thread.join()


def props(component: object) -> dict[str, Any]:
    """
    Extract a rendered Dash component's props.

    dash-mantine-components ships no py.typed marker, so its component attrs are Unknown to
    pyright regardless of how this is typed.
    """
    return cast("Any", component).to_plotly_json()["props"]


def find_props(component: Any, target_id: object) -> dict[str, Any] | None:  # noqa: ANN401
    """Depth-first search a dash component tree for the props of a node with `target_id`."""
    if isinstance(component, list):
        for item in cast("list[Any]", component):
            found = find_props(item, target_id)
            if found is not None:
                return found
        return None
    if not hasattr(component, "to_plotly_json"):
        return None
    component_props = props(component)
    if component_props.get("id") == target_id:
        return component_props
    children = component_props.get("children")
    return find_props(children, target_id) if children is not None else None
