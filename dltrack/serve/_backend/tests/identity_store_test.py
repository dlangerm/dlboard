"""The identity half of the store: who a user is, what they're granted, and the credentials they hold."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dltrack import models

if TYPE_CHECKING:
    from dltrack.plugins.data_stores.sqlite import SQLLiteStore


def _principal(
    subject: str, *, issuer: str = "idp", username: str | None = None, **kwargs: object
) -> models.Principal:
    return models.Principal.model_validate(
        {"issuer": issuer, "subject": subject, "username": username or subject, **kwargs}
    )


def test_a_user_is_its_issuer_and_subject_and_signing_in_again_refreshes_its_profile(
    store: SQLLiteStore,
) -> None:
    first = store.get_or_create_user(_principal("sub-1", username="alice"))
    again = store.get_or_create_user(
        _principal("sub-1", username="alice", email="a@x.io", groups=frozenset({"ml"}))
    )

    assert again.id == first.id
    assert (again.email, again.groups) == ("a@x.io", ["ml"])
    assert store.get_user(first.id) == again
    assert store.find_user("alice") == again
    assert store.get_user(first.id + 1000) is None
    assert store.find_user("nobody") is None


def test_a_taken_username_is_never_shared_between_identities(store: SQLLiteStore) -> None:
    store.get_or_create_user(_principal("sub-1", username="alice"))

    with pytest.raises(ValueError, match="already taken"):
        store.get_or_create_user(_principal("sub-2", username="alice", issuer="other-idp"))


def test_only_an_unverified_first_user_claims_the_bootstrap_admin_grant(store: SQLLiteStore) -> None:
    verified = store.get_or_create_user(_principal("sub-1"))
    unverified = store.get_or_create_user(models.Principal.unverified("local"))

    assert verified.scopes == []
    assert unverified.scopes == [models.Scope.ALL]


def test_a_user_can_be_disabled_and_have_its_sessions_revoked(store: SQLLiteStore) -> None:
    user = store.get_or_create_user(_principal("sub-1"))

    updated = store.update_user(
        user.model_copy(
            update={"scopes": [models.Scope.USER_MANAGE], "session_epoch": 1, "disabled_at": user.created_at}
        )
    )

    assert store.get_user(user.id) == updated
    assert (updated.session_epoch, updated.disabled_at) == (1, user.created_at)


def test_a_grant_replaces_the_same_grantees_earlier_role_and_reaches_them_by_user_or_group(
    store: SQLLiteStore,
) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    user = store.get_or_create_user(_principal("sub-1", groups=frozenset({"ml"})))
    by_user = models.NewProjectGrant(project_id=project.id, user_id=user.id, role=models.ProjectRole.VIEWER)
    by_group = models.NewProjectGrant(project_id=project.id, group="ml", role=models.ProjectRole.EDITOR)

    store.set_project_grant(by_user)
    upgraded = store.set_project_grant(by_user.model_copy(update={"role": models.ProjectRole.OWNER}))
    group_grant = store.set_project_grant(by_group)

    assert store.list_project_grants([project.id]) == [upgraded, group_grant]
    assert {g.role for g in store.grants_for(user)} == {models.ProjectRole.OWNER, models.ProjectRole.EDITOR}
    assert store.grants_for(store.get_or_create_user(_principal("sub-2"))) == []

    store.delete_project_grant(project.id, upgraded.id)
    assert [g.id for g in store.grants_for(user)] == [group_grant.id]


def test_a_project_remembers_its_everyone_role(store: SQLLiteStore) -> None:
    project = store.create_project(
        models.NewProject(name="p", description="d", everyone_role=models.ProjectRole.VIEWER)
    )

    stored = store.get_project(project.id)

    assert stored.everyone_role == models.ProjectRole.VIEWER
    assert models.ProjectRole.OWNER.includes(models.ProjectRole.EDITOR)
    assert not models.ProjectRole.VIEWER.includes(models.ProjectRole.EDITOR)


def test_an_api_token_is_looked_up_by_its_public_id_and_can_be_used_then_revoked(store: SQLLiteStore) -> None:
    owner = store.get_or_create_user(_principal("sub-1"))
    other = store.get_or_create_user(_principal("sub-2"))
    token = store.create_api_token(
        models.NewApiToken(user_id=owner.id, name="jobs", token_id="abc", secret_hash="h", expires_at=None)
    )

    store.touch_api_token(token.id)
    store.revoke_api_token(token.id, other.id)  # someone else's token is left alone
    used = store.get_api_token("abc")
    assert used is not None
    assert (used.last_used_at is not None, used.revoked_at, used.expires_at, used.secret_hash) == (
        True,
        None,
        None,
        "h",
    )
    assert store.get_api_token("missing") is None

    store.revoke_api_token(token.id, owner.id)
    assert [t.token_id for t in store.list_api_tokens(owner.id)] == ["abc"]
    assert store.list_api_tokens(owner.id)[0].revoked_at is not None
    assert store.list_api_tokens(other.id) == []


def test_setting_a_password_replaces_the_previous_one(store: SQLLiteStore) -> None:
    user = store.get_or_create_user(_principal("sub-1"))
    assert store.get_password_credential(user.id) is None

    store.set_password_credential(models.PasswordCredential(user_id=user.id, password_hash="one"))
    store.set_password_credential(models.PasswordCredential(user_id=user.id, password_hash="two"))

    credential = store.get_password_credential(user.id)
    assert credential is not None
    assert (credential.password_hash, credential.updated_at.year >= 2026) == ("two", True)


def test_a_run_is_fetched_by_id_until_it_is_deleted(store: SQLLiteStore, experiment_id: int) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    actor = store.get_or_create_user(models.Principal.unverified("alice"))

    assert store.get_run(run.id) == run
    store.delete_run(run.id, actor)

    assert store.get_run(run.id) is None


def test_new_users_start_from_unrelated_session_epochs_so_a_recreated_database_cannot_honor_old_sessions(
    store: SQLLiteStore,
) -> None:
    users = [store.get_or_create_user(_principal(f"sub-{i}")) for i in range(5)]

    assert len({u.session_epoch for u in users}) > 1
    assert all(store.get_user(u.id) == u for u in users)
