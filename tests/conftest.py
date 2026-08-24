# pyright: reportPrivateUsage=false
"""Shared fixtures and Dash-component helpers for the test suite."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from dltrack import models
from dltrack.plugins.data_stores.sqlite import SQLLiteStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def store(tmp_path: Path) -> SQLLiteStore:
    return SQLLiteStore(tmp_path / "test.sqlite")


@pytest.fixture
def experiment_id(store: SQLLiteStore) -> int:
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    return experiment.id


def props(component: object) -> dict[str, Any]:
    """Extract a rendered Dash component's props.

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
