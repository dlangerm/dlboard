"""Who may read and write a plugin's scoped setting: the rule it registered, its check, and nobody without one."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from dlboard import models
from dlboard._compat import StrEnum
from dlboard.conftest import recorded_statements
from dlboard.models import ProjectRole, Scope
from dlboard.serve._backend._authorization import AuthorizingDataStore
from dlboard.serve._backend._setting_rules import SettingRule, SettingWrite

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


class _Key(StrEnum):
    RULE = "plugin.rule"


def _capped(write: SettingWrite) -> dict[str, Any]:
    """At most 10, unless an admin says otherwise, which the stored value then records."""
    if write.value["n"] > 10:
        if not write.admin:
            msg = "past the cap"
            raise PermissionError(msg)
        return {**write.value, "past_cap": True}
    return write.value


_RULES = {str(_Key.RULE): SettingRule(key=_Key.RULE, check=_capped)}


def _user(store: SQLLiteStore, name: str, *scopes: Scope) -> models.User:
    user = store.get_or_create_user(models.Principal(issuer="test", subject=name, username=name))
    return store.update_user(user.model_copy(update={"scopes": list(scopes)}))


def _as(store: SQLLiteStore, user: models.User) -> AuthorizingDataStore:
    return AuthorizingDataStore(store, user, enforce_grants=True, setting_rules=_RULES)


@pytest.mark.parametrize(
    ("role", "reads", "writes_experiment", "writes_project"),
    [
        (None, False, False, False),
        (ProjectRole.VIEWER, True, False, False),
        (ProjectRole.EDITOR, True, True, False),
        (ProjectRole.OWNER, True, True, True),
    ],
)
def test_a_setting_s_rule_says_who_reads_it_and_who_writes_each_scope(
    store: SQLLiteStore,
    role: ProjectRole | None,
    *,
    reads: bool,
    writes_experiment: bool,
    writes_project: bool,
) -> None:
    owner, bob = _user(store, "owner"), _user(store, "bob")
    project = _as(store, owner).create_project(models.NewProject(name="p", description="")).id
    experiment = store.create_experiment(models.NewExperiment(project_id=project)).id
    if role is not None:
        store.set_project_grant(models.NewProjectGrant(project_id=project, user_id=bob.id, role=role))
    as_bob = _as(store, bob)
    for allowed, act in (
        (reads, lambda: as_bob.get_scoped_settings(_Key.RULE, project)),
        (writes_experiment, lambda: as_bob.put_scoped_setting(_Key.RULE, project, experiment, {"n": 1}, bob)),
        (writes_project, lambda: as_bob.put_scoped_setting(_Key.RULE, project, None, {"n": 1}, bob)),
    ):
        if allowed:
            act()
        else:
            with pytest.raises(PermissionError):
                act()


def test_a_setting_s_check_holds_a_writer_to_it_and_records_an_admin_past_it(store: SQLLiteStore) -> None:
    owner, admin = _user(store, "owner"), _user(store, "admin", Scope.ALL)
    project = _as(store, owner).create_project(models.NewProject(name="p", description="")).id
    with pytest.raises(PermissionError, match="past the cap"):
        _as(store, owner).put_scoped_setting(_Key.RULE, project, None, {"n": 11}, owner)
    saved = _as(store, admin).put_scoped_setting(_Key.RULE, project, None, {"n": 11}, admin)
    assert saved is not None
    assert saved.value == {"n": 11, "past_cap": True}


def test_a_setting_no_plugin_registered_a_rule_for_is_the_system_s_alone(store: SQLLiteStore) -> None:
    admin = _user(store, "admin", Scope.ALL)
    project = store.create_project(models.NewProject(name="p", description="")).id
    with pytest.raises(PermissionError, match="no plugin lets a request use"):
        _as(store, admin).get_scoped_settings("unregistered", project)


def test_without_enforced_grants_an_admin_is_the_owner_of_kept_runs_and_of_project_settings(
    store: SQLLiteStore,
) -> None:
    """The local deployment's one user is the bootstrap admin, and every caller an editor: only the admin owns."""
    admin, editor = _user(store, "admin", Scope.ALL), _user(store, "editor", Scope.RUN_DELETE)
    project = store.create_project(models.NewProject(name="p", description="")).id
    experiment = store.create_experiment(models.NewExperiment(project_id=project)).id
    run = store.create_run(models.NewRun(experiment_id=experiment)).id

    def as_(user: models.User) -> AuthorizingDataStore:
        return AuthorizingDataStore(store, user, enforce_grants=False, setting_rules=_RULES)

    as_(editor).keep_runs(frozenset({run}), editor)
    as_(editor).put_scoped_setting(_Key.RULE, project, experiment, {"n": 1}, editor)
    for refused in (
        lambda: as_(editor).release_runs(frozenset({run}), editor),
        lambda: as_(editor).delete_run(run, editor),
        lambda: as_(editor).put_scoped_setting(_Key.RULE, project, None, {"n": 1}, editor),
    ):
        with pytest.raises(PermissionError):
            refused()
    as_(admin).put_scoped_setting(_Key.RULE, project, None, {"n": 1}, admin)
    as_(admin).release_runs(frozenset({run}), admin)
    as_(editor).delete_run(run, editor)


def test_a_check_is_told_what_the_scope_holds_now_and_a_bulk_read_shows_only_the_projects_a_reader_may_see(
    store: SQLLiteStore,
) -> None:
    seen: list[dict[str, Any] | None] = []

    def watching(write: SettingWrite) -> dict[str, Any]:
        seen.append(write.stored)
        return write.value

    rules = {str(_Key.RULE): SettingRule(key=_Key.RULE, check=watching)}
    owner, bob = _user(store, "owner"), _user(store, "bob")
    as_owner = AuthorizingDataStore(store, owner, enforce_grants=True, setting_rules=rules)
    as_bob = AuthorizingDataStore(store, bob, enforce_grants=True, setting_rules=rules)
    mine, shared = (as_owner.create_project(models.NewProject(name=n, description="")).id for n in "ms")
    store.set_project_grant(
        models.NewProjectGrant(project_id=shared, user_id=bob.id, role=ProjectRole.VIEWER)
    )
    for project in (mine, shared):
        as_owner.put_scoped_setting(_Key.RULE, project, None, {"n": 1}, owner)
        as_owner.put_scoped_setting(_Key.RULE, project, None, {"n": 2}, owner)
    assert seen == [None, {"n": 1}, None, {"n": 1}]
    assert sorted(as_owner.all_scoped_settings(_Key.RULE)) == sorted([mine, shared])
    assert list(as_bob.all_scoped_settings(_Key.RULE)) == [shared]


def test_a_request_reads_a_projects_settings_once_and_a_write_of_its_own_is_seen(store: SQLLiteStore) -> None:
    """A page draws a project's header and its sections apart, each reading the same setting (#558)."""
    owner = _user(store, "owner")
    as_owner = _as(store, owner)
    project = as_owner.create_project(models.NewProject(name="p", description="")).id
    with recorded_statements(store._engine) as statements:
        assert as_owner.get_scoped_settings(_Key.RULE, project).project is None
        assert as_owner.get_scoped_settings(_Key.RULE, project).project is None
        reads = [s for s in statements if s.sql.lstrip().startswith("SELECT") and "ScopedSetting" in s.sql]
        assert len(reads) == 1
        as_owner.put_scoped_setting(_Key.RULE, project, None, {"n": 1}, owner)
        written = as_owner.get_scoped_settings(_Key.RULE, project).project
        assert written is not None
        assert written.value == {"n": 1}
