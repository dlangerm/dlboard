# pyright: reportPrivateUsage=false
"""Shared fixtures and Dash-component helpers for the test suite."""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING, Any, cast

import pytest

from dltrack import models
from dltrack.plugins.data_stores.sqlite import SQLLiteStore

if TYPE_CHECKING:
    from pathlib import Path


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
