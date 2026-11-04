"""Who may hold a project past the server's retention cap, and an admin's say over kept runs where grants are not enforced."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum
import pytest

from dlboard import models
from dlboard.models import ProjectRole, Scope
from dlboard.serve._backend._authorization import AuthorizingDataStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


def _user(store: SQLLiteStore, name: str, *scopes: Scope) -> models.User:
    user = store.get_or_create_user(models.Principal(issuer="test", subject=name, username=name))
    return store.update_user(user.model_copy(update={"scopes": list(scopes)}))


@pytest.mark.parametrize(
    ("rule", "past_cap"),
    [
        (models.RunCount(max_runs=40), False),
        (models.RunCount(max_runs=51), True),
        (models.Unlimited(), True),
        (models.RunAge(max_age_days=1), True),
    ],
    ids=["within", "more runs", "every run", "an uncapped kind"],
)
def test_only_an_admin_holds_a_project_past_the_cap_and_the_policy_says_so(
    store: SQLLiteStore, monkeypatch: pytest.MonkeyPatch, rule: models.RetentionRule, *, past_cap: bool
) -> None:
    monkeypatch.setenv("DLBOARD_RETENTION_MAX_RUNS", "50")
    owner, admin = _user(store, "owner"), _user(store, "admin", Scope.ALL)
    project = AuthorizingDataStore(store, owner, enforce_grants=True).create_project(
        models.NewProject(name="p", description="")
    )
    assert store.get_retention(project.id).project.active is not None

    as_owner = AuthorizingDataStore(store, owner, enforce_grants=True)
    if past_cap:
        with pytest.raises(PermissionError):
            as_owner.set_retention_policy(project.id, None, rule, owner)
        saved = AuthorizingDataStore(store, admin, enforce_grants=True).set_retention_policy(
            project.id, None, rule, admin
        )
    else:
        saved = as_owner.set_retention_policy(project.id, None, rule, owner)
    assert saved.pending is not None
    assert saved.pending.cap_exempt is past_cap
    assert as_owner.role_on(project.id) is ProjectRole.OWNER


def test_without_enforced_grants_an_admin_is_the_owner_of_kept_runs_and_project_rules(
    store: SQLLiteStore,
) -> None:
    """The local deployment's one user is the bootstrap admin, and every caller an editor: only the admin releases."""
    admin, editor = _user(store, "admin", Scope.ALL), _user(store, "editor", Scope.RUN_DELETE)
    project = store.create_project(models.NewProject(name="p", description=""))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    run = store.create_run(models.NewRun(experiment_id=experiment.id)).id
    as_admin, as_editor = (AuthorizingDataStore(store, u, enforce_grants=False) for u in (admin, editor))

    as_editor.keep_runs(frozenset({run}), editor)
    for refused in (
        lambda: as_editor.release_runs(frozenset({run}), editor),
        lambda: as_editor.delete_run(run, editor),
        lambda: as_editor.set_retention_policy(project.id, None, models.RunAge(max_age_days=1), editor),
    ):
        with pytest.raises(PermissionError):
            refused()
    as_editor.set_retention_policy(project.id, experiment.id, models.RunAge(max_age_days=1), editor)
    as_admin.set_retention_policy(project.id, None, models.RunAge(max_age_days=1), admin)
    as_admin.release_runs(frozenset({run}), admin)
    as_editor.delete_run(run, editor)


def test_enforcing_retention_is_the_purge_scope_s_not_an_owner_s(store: SQLLiteStore) -> None:
    """The worker's own reads and records: no request makes them, whatever its role."""
    owner, purger = _user(store, "owner"), _user(store, "purger", Scope.PURGE)
    project = AuthorizingDataStore(store, owner, enforce_grants=True).create_project(
        models.NewProject(name="p", description="")
    )
    now = pendulum.now("UTC")
    calls: list[Callable[[AuthorizingDataStore], object]] = [
        lambda s: s.retention_candidates(project.id, limit=10),
        lambda s: s.record_retention_status(project.id, overdue_runs=0),
        lambda s: s.claim_retention_lease("me", until=now),
        lambda s: s.record_retention_sweep(),
        lambda s: s.trashed_before(models.EntityType.RUN, now, limit=10),
    ]
    for call in calls:
        with pytest.raises(PermissionError):
            call(AuthorizingDataStore(store, owner, enforce_grants=True))
        call(AuthorizingDataStore(store, purger, enforce_grants=True))
