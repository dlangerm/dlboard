"""The experiment page's breadcrumb offers the project's newest experiments, and the one on screen among them (#590)."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import dash_mantine_components as dmc
import pytest

from dlboard import models
from dlboard.serve.app import _SWITCHER_EXPERIMENTS, _experiment_switcher

if TYPE_CHECKING:
    from dlboard.serve._backend._sql_store_base import SQLStoreBase


@pytest.fixture(autouse=True)
def _plain_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Links as plain paths: `relative_path` reads the config of a Dash app this test does not build."""
    # The module, not `dlboard.serve.app`, which `dlboard.serve` re-exports as the function that builds an app.
    monkeypatch.setattr(sys.modules[_experiment_switcher.__module__], "relative_path", str)


@pytest.mark.parametrize("viewing", [2, 18], ids=["an-old-one", "a-new-one"])
def test_the_switcher_offers_the_newest_experiments_and_the_one_on_screen(
    store: SQLStoreBase[Any], viewing: int
) -> None:
    project = store.create_project(models.NewProject(name="p", description=""))
    experiments = [
        store.create_experiment(models.NewExperiment(project_id=project.id, name=f"e{i}")) for i in range(20)
    ]

    menu = _experiment_switcher(store, project, experiments[viewing].id, f"e{viewing}")

    items = [item for item in menu._traverse() if isinstance(item, dmc.MenuItem)]  # type: ignore[attr-defined]
    offered = [str(item.children) for item in items[:-1]]  # the last is "All experiments"
    newest = [f"e{i}" for i in range(19, 19 - _SWITCHER_EXPERIMENTS, -1)]
    assert offered == newest + ([] if f"e{viewing}" in newest else [f"e{viewing}"])
