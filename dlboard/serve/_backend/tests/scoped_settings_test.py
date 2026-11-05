"""Plugins' scoped settings on every backend: one a key and scope, replaced, deleted, and gone with what they are of."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend

if TYPE_CHECKING:
    from dlboard.serve import SQLStoreBase


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


def _values(
    store: SQLStoreBase[Any], project_id: int, key: str = "plugin.rule"
) -> tuple[Any, dict[int, Any]]:
    settings = store.get_scoped_settings(key, project_id)
    own = settings.project.value if settings.project is not None else None
    return own, {e: s.value for e, s in settings.experiments.items()}


def test_a_setting_is_one_a_key_and_scope_and_saving_again_replaces_it(
    store: SQLStoreBase[Any], admin: models.User
) -> None:
    project = store.create_project(models.NewProject(name="p", description="")).id
    first, second = (store.create_experiment(models.NewExperiment(project_id=project)).id for _ in range(2))
    store.put_scoped_setting("plugin.rule", project, None, {"n": 1}, admin)
    store.put_scoped_setting("plugin.rule", project, None, {"n": 2}, admin)
    store.put_scoped_setting("plugin.rule", project, first, {"n": 0}, admin)
    store.put_scoped_setting("plugin.rule", project, first, {"n": 3}, admin)
    store.put_scoped_setting("plugin.other", project, second, {"n": 4}, admin)
    assert _values(store, project) == ({"n": 2}, {first: {"n": 3}})
    assert _values(store, project, "plugin.other") == (None, {second: {"n": 4}})
    assert store.put_scoped_setting("plugin.rule", project, None, None, admin) is None
    assert _values(store, project) == (None, {first: {"n": 3}})


def test_a_setting_is_never_of_another_project_s_experiment_and_goes_with_what_it_is_of(
    store: SQLStoreBase[Any], admin: models.User
) -> None:
    project, other = (store.create_project(models.NewProject(name=n, description="")).id for n in "ab")
    experiment = store.create_experiment(models.NewExperiment(project_id=project)).id
    elsewhere = store.create_experiment(models.NewExperiment(project_id=other)).id
    with pytest.raises(ValueError, match="is not an experiment of project"):
        store.put_scoped_setting("plugin.rule", project, elsewhere, {}, admin)
    store.put_scoped_setting("plugin.rule", project, experiment, {"n": 1}, admin)
    store.put_scoped_setting("plugin.rule", project, None, {"n": 0}, admin)
    store.delete_experiment(experiment, admin)
    assert _values(store, project) == ({"n": 0}, {})
    store.delete_project(project, admin)
    store.purge_project(project, admin)
    assert store._execute(store.tables[models.ScopedSetting].select()) == []


def test_one_read_gives_every_projects_setting_but_a_deleted_one_s_or_a_deleted_experiment_s(
    store: SQLStoreBase[Any], admin: models.User
) -> None:
    gone, kept = (
        store.create_project(models.NewProject(name=n, description="")).id for n in ("gone", "kept")
    )
    live, dead = (store.create_experiment(models.NewExperiment(project_id=kept)).id for _ in range(2))
    for project, experiment in ((gone, None), (kept, None), (kept, live), (kept, dead)):
        store.put_scoped_setting("plugin.rule", project, experiment, {"n": 1}, admin)
    store.put_scoped_setting("plugin.other", kept, None, {"n": 2}, admin)
    store.delete_project(gone, admin)
    store.delete_experiment(dead, admin)
    everything = store.all_scoped_settings("plugin.rule")
    assert {p: (s.project and s.project.value, set(s.experiments)) for p, s in everything.items()} == {
        kept: ({"n": 1}, {live})
    }
