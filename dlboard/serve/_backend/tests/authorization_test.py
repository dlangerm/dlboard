"""Tests for `AuthorizingDataStore`/`AuthorizingArtifactStore`: the per-request access-control wrappers."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

import pendulum
import pytest
from pydantic import AnyUrl

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend, create_entity_chain
from dlboard.models import ProjectRole, Scope
from dlboard.plugins.data_stores._blob_store import BlobArtifactStore, blob_key
from dlboard.plugins.data_stores.filesystem import FSBlobs
from dlboard.serve._backend._authorization import AuthorizingArtifactStore, AuthorizingDataStore
from dlboard.serve._pages._experiment._experiment_page_state import BasicExperimentPage

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    """Every rule below rides on the store's grant queries -- check them on every backend."""
    return request.param


def _protocol_members(protocol: type) -> set[str]:
    return {name for name, _ in inspect.getmembers(protocol) if not name.startswith("_")}


@pytest.mark.parametrize(
    ("protocol", "wrapper"),
    [(models.DataStore, AuthorizingDataStore), (models.ArtifactStore, AuthorizingArtifactStore)],
)
def test_every_protocol_method_has_an_explicit_rule(protocol: type, wrapper: type) -> None:
    """A store method added without a decision about who may call it would silently pass through."""
    missing = _protocol_members(protocol) - set(vars(wrapper))
    assert not missing, f"{wrapper.__name__} must decide who may call: {sorted(missing)}"


def _user(store: SQLLiteStore, name: str, *scopes: Scope) -> models.User:
    user = store.get_or_create_user(models.Principal(issuer="test", subject=name, username=name))
    return store.update_user(user.model_copy(update={"scopes": list(scopes)})) if scopes else user


def _as(store: SQLLiteStore, user: models.User, *, enforce: bool = True) -> AuthorizingDataStore:
    return AuthorizingDataStore(store, user, enforce_grants=enforce)


# -- Site-wide scopes (enforced with or without project grants) ----------------------------------


@pytest.mark.parametrize("enforce", [True, False])
@pytest.mark.parametrize(
    ("method", "scope"),
    [("restore_project", Scope.RESTORE), ("purge_project", Scope.PURGE)],
)
def test_restore_and_purge_need_their_scope(
    store: SQLLiteStore, enforce: bool, method: str, scope: Scope
) -> None:
    owner = _user(store, "owner", Scope.PROJECT_DELETE)
    project = _as(store, owner, enforce=enforce).create_project(models.NewProject(name="p", description=""))
    store.delete_project(project.id, owner)

    with pytest.raises(PermissionError):
        getattr(_as(store, owner, enforce=enforce), method)(project.id, owner)
    scoped = _user(store, "scoped", scope)
    getattr(_as(store, scoped, enforce=enforce), method)(project.id, scoped)


@pytest.mark.parametrize(
    ("method", "scope"),
    [
        ("delete_project", Scope.PROJECT_DELETE),
        ("delete_experiment", Scope.EXPERIMENT_DELETE),
        ("delete_run", Scope.RUN_DELETE),
        ("delete_artifact", Scope.ARTIFACT_DELETE),
    ],
)
def test_without_enforced_grants_deletes_stay_scope_gated(
    store: SQLLiteStore, method: str, scope: Scope
) -> None:
    """The local, anonymous experience: everyone edits, but deleting still takes the scope it always did."""
    chain = create_entity_chain(store, artifact=True)
    entity_id = {
        "delete_project": chain.project_id,
        "delete_experiment": chain.experiment_id,
        "delete_run": chain.run_id,
        "delete_artifact": chain.artifact_id,
    }[method]
    nobody = _user(store, "nobody")
    with pytest.raises(PermissionError, match="lacks"):
        getattr(_as(store, nobody, enforce=False), method)(entity_id, nobody)

    scoped = _user(store, "scoped", scope)
    getattr(_as(store, scoped, enforce=False), method)(entity_id, scoped)


def test_audit_log_needs_its_scope(store: SQLLiteStore) -> None:
    nobody, reader = _user(store, "nobody"), _user(store, "reader", Scope.AUDIT_LOG_READ)

    with pytest.raises(PermissionError, match="lacks"):
        list(_as(store, nobody).list_audit_log(nobody))
    assert list(_as(store, reader).list_audit_log(reader)) == []


def test_a_store_cannot_act_as_anyone_but_its_actor(store: SQLLiteStore) -> None:
    admin, mallory = _user(store, "admin", Scope.ALL), _user(store, "mallory")
    project = _as(store, mallory).create_project(models.NewProject(name="p", description=""))

    with pytest.raises(PermissionError, match="cannot act as"):
        _as(store, mallory).delete_project(project.id, admin)


# -- Project roles ------------------------------------------------------------------------------


type _Chain = tuple[models.Project, models.Experiment, models.Run]


def _shared_chain(store: SQLLiteStore, role: ProjectRole | None) -> tuple[models.User, _Chain]:
    """Alice's project/experiment/run, with bob holding `role` on it (or nothing)."""
    alice, bob = _user(store, "alice"), _user(store, "bob")
    project = _as(store, alice).create_project(models.NewProject(name="p", description=""))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    run = store.create_run(models.NewRun(experiment_id=experiment.id))
    if role is not None:
        store.set_project_grant(models.NewProjectGrant(project_id=project.id, user_id=bob.id, role=role))
    return bob, (project, experiment, run)


def _metric(experiment_id: int, run_id: int) -> models.LoggedMetrics:
    return models.LoggedMetrics(
        metrics={"loss": 1.0},
        step=0,
        experiment_id=experiment_id,
        run_id=run_id,
        timestamp_utc=pendulum.now(),
    )


_OPERATIONS: dict[str, tuple[ProjectRole, Callable[[AuthorizingDataStore, _Chain], object]]] = {
    "list experiments": (ProjectRole.VIEWER, lambda s, c: list(s.get_experiments(c[0].id))),
    "read metrics": (ProjectRole.VIEWER, lambda s, c: s.fetch_metrics(c[1].id)),
    "create a run": (ProjectRole.EDITOR, lambda s, c: s.create_run(models.NewRun(experiment_id=c[1].id))),
    "log metrics": (ProjectRole.EDITOR, lambda s, c: s.log_metrics([_metric(c[1].id, c[2].id)])),
    "finish a run": (
        ProjectRole.EDITOR,
        lambda s, c: s.finish_run(c[2].id, models.RunStatus.FINISHED, pendulum.now("UTC")),
    ),
    "delete an experiment": (ProjectRole.EDITOR, lambda s, c: s.delete_experiment(c[1].id, s.actor)),
    "share the project": (
        ProjectRole.OWNER,
        lambda s, c: s.update_project(c[0].model_copy(update={"everyone_role": ProjectRole.VIEWER})),
    ),
    "delete the project": (ProjectRole.OWNER, lambda s, c: s.delete_project(c[0].id, s.actor)),
}


@pytest.mark.parametrize("held", [None, *ProjectRole])
@pytest.mark.parametrize("operation", list(_OPERATIONS))
def test_each_operation_needs_its_project_role(
    store: SQLLiteStore, held: ProjectRole | None, operation: str
) -> None:
    required, run_operation = _OPERATIONS[operation]
    bob, chain = _shared_chain(store, held)
    as_bob = _as(store, bob)

    if held is not None and held.includes(required):
        run_operation(as_bob, chain)
    else:
        with pytest.raises(PermissionError):
            run_operation(as_bob, chain)


def test_a_project_you_cannot_see_reads_as_missing(store: SQLLiteStore) -> None:
    bob, (project, experiment, run) = _shared_chain(store, None)
    as_bob = _as(store, bob)

    assert list(as_bob.get_projects()) == []
    assert as_bob.get_project_stats() == {}
    assert as_bob.get_experiment(experiment.id) is None
    assert as_bob.get_run(run.id) is None
    assert project.id not in {p.id for p in as_bob.get_projects()}


def _share_with_everyone(store: SQLLiteStore, project: models.Project, _bob: models.User) -> None:
    store.update_project(project.model_copy(update={"everyone_role": ProjectRole.VIEWER}))


def _share_with_bobs_group(store: SQLLiteStore, project: models.Project, bob: models.User) -> None:
    store.set_project_grant(
        models.NewProjectGrant(project_id=project.id, group="ml-team", role=ProjectRole.VIEWER)
    )
    store.update_user(bob.model_copy(update={"groups": ["ml-team"]}))


@pytest.mark.parametrize("share", [_share_with_everyone, _share_with_bobs_group])
def test_a_project_can_be_shared_with_everyone_or_a_group(
    store: SQLLiteStore, share: Callable[[SQLLiteStore, models.Project, models.User], None]
) -> None:
    bob, (project, experiment, _run) = _shared_chain(store, None)

    share(store, project, bob)
    as_bob = _as(store, store.get_user(bob.id) or bob)

    assert [p.id for p in as_bob.get_projects()] == [project.id]
    assert as_bob.get_experiment(experiment.id) is not None
    with pytest.raises(PermissionError):
        as_bob.create_run(models.NewRun(experiment_id=experiment.id))


def test_new_projects_start_at_the_deployments_default_access(store: SQLLiteStore) -> None:
    alice = _user(store, "alice")
    as_alice = AuthorizingDataStore(store, alice, enforce_grants=True, new_project_access=ProjectRole.VIEWER)

    project = as_alice.create_project(models.NewProject(name="p", description=""))

    assert project.everyone_role is ProjectRole.VIEWER
    assert as_alice.role_on(project.id) is ProjectRole.OWNER


def test_logging_to_a_run_of_another_experiment_is_refused(store: SQLLiteStore) -> None:
    """Editing your own experiment never reaches into someone else's run by claiming its id."""
    bob, (_project, _experiment, alices_run) = _shared_chain(store, None)
    own = _as(store, bob).create_project(models.NewProject(name="mine", description=""))
    own_experiment = store.create_experiment(models.NewExperiment(project_id=own.id))

    with pytest.raises(PermissionError, match="does not belong"):
        _as(store, bob).log_metrics([_metric(own_experiment.id, alices_run.id)])


# -- Get-or-create by name ------------------------------------------------------------------------


def test_get_or_create_project_is_scoped_to_what_you_can_write(store: SQLLiteStore) -> None:
    alice, bob = _user(store, "alice"), _user(store, "bob")
    alices = _as(store, alice).get_or_create_project("mnist")

    bobs = _as(store, bob).get_or_create_project("mnist")

    assert bobs.id != alices.id
    assert _as(store, bob).get_or_create_project("mnist").id == bobs.id


def test_get_or_create_project_refuses_an_ambiguous_name(store: SQLLiteStore) -> None:
    bob, (project, _experiment, _run) = _shared_chain(store, ProjectRole.EDITOR)
    _as(store, bob).create_project(models.NewProject(name=project.name, description=""))

    with pytest.raises(models.AmbiguousProjectError):
        _as(store, bob).get_or_create_project(project.name)


# -- Users, views, and notes ----------------------------------------------------------------------


def test_list_users_only_shows_people_you_share_a_project_with(store: SQLLiteStore) -> None:
    bob, _chain = _shared_chain(store, ProjectRole.VIEWER)
    _user(store, "stranger")

    assert {u.username for u in _as(store, bob).list_users()} == {"alice", "bob"}
    assert len(_as(store, _user(store, "root", Scope.USER_MANAGE)).list_users()) == 4


def test_user_manage_alone_cannot_grant_all(store: SQLLiteStore) -> None:
    """
    Regression: `USER_MANAGE` used to be enough to grant `Scope.ALL` to anyone, including yourself
    -- a strictly weaker scope in name only, since its holder could self-escalate to full admin.
    """
    manager = _user(store, "manager", Scope.USER_MANAGE)
    target = _user(store, "target")

    with pytest.raises(PermissionError):
        _as(store, manager).update_user(target.model_copy(update={"scopes": [Scope.ALL]}))


def test_user_manage_alone_cannot_modify_an_existing_all_admin(store: SQLLiteStore) -> None:
    """Not even an unrelated field -- disabling a real admin needs `ALL`, not just `USER_MANAGE`."""
    manager = _user(store, "manager", Scope.USER_MANAGE)
    admin = _user(store, "admin", Scope.ALL)

    with pytest.raises(PermissionError):
        _as(store, manager).update_user(admin.model_copy(update={"groups": ["anything"]}))


def test_all_can_grant_all_and_modify_an_existing_all_admin(store: SQLLiteStore) -> None:
    root = _user(store, "root", Scope.ALL)
    target = _user(store, "target")
    other_admin = _user(store, "other-admin", Scope.ALL)

    granted = _as(store, root).update_user(target.model_copy(update={"scopes": [Scope.ALL]}))
    modified = _as(store, root).update_user(other_admin.model_copy(update={"groups": ["ml-team"]}))

    assert Scope.ALL in granted.scopes
    assert modified.groups == ["ml-team"]


def test_user_manage_can_still_update_an_ordinary_user(store: SQLLiteStore) -> None:
    manager = _user(store, "manager", Scope.USER_MANAGE)
    target = _user(store, "target")

    updated = _as(store, manager).update_user(target.model_copy(update={"groups": ["ml-team"]}))

    assert updated.groups == ["ml-team"]


def test_a_viewer_can_change_shared_viewing_state_but_not_the_layout(store: SQLLiteStore) -> None:
    bob, (_project, experiment, _run) = _shared_chain(store, ProjectRole.VIEWER)
    shared = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment.id)
    as_bob = _as(store, bob)

    as_bob.update_page(shared.model_copy(update={"page_settings": {"open_panel": ["p"]}}))
    with pytest.raises(PermissionError):
        as_bob.update_page(shared.model_copy(update={"panels": [models.PanelInstance[Any, Any](name="p")]}))


def test_only_a_views_owner_can_change_it(store: SQLLiteStore) -> None:
    bob, (_project, experiment, _run) = _shared_chain(store, ProjectRole.EDITOR)
    alice = store.find_user("alice")
    assert alice is not None
    view = _as(store, alice).create_view(
        BasicExperimentPage,
        models.NewPage[Any, Any](experiment_id=experiment.id, owner_id=alice.id, name="mine", shared=True),
    )

    assert _as(store, bob).get_view(BasicExperimentPage, view.id) is not None
    with pytest.raises(PermissionError, match="owner"):
        _as(store, bob).update_page(view.model_copy(update={"name": "stolen"}))
    with pytest.raises(PermissionError):
        _as(store, bob).add_comment(
            models.NewComment(experiment_id=experiment.id, author_id=alice.id, body="hi")
        )


def test_a_private_view_is_invisible_to_everyone_but_its_owner(store: SQLLiteStore) -> None:
    bob, (_project, experiment, _run) = _shared_chain(store, ProjectRole.VIEWER)
    alice = store.find_user("alice")
    assert alice is not None
    view = _as(store, alice).create_view(
        BasicExperimentPage,
        models.NewPage[Any, Any](experiment_id=experiment.id, owner_id=alice.id, name="private"),
    )

    assert _as(store, alice).get_view(BasicExperimentPage, view.id) is not None
    assert _as(store, bob).get_view(BasicExperimentPage, view.id) is None
    assert view.id not in {v.id for v in _as(store, bob).list_views(experiment.id, bob.id)}


def test_a_shared_view_is_visible_to_every_project_viewer(store: SQLLiteStore) -> None:
    bob, (_project, experiment, _run) = _shared_chain(store, ProjectRole.VIEWER)
    alice = store.find_user("alice")
    assert alice is not None
    view = _as(store, alice).create_view(
        BasicExperimentPage,
        models.NewPage[Any, Any](experiment_id=experiment.id, owner_id=alice.id, name="shared", shared=True),
    )

    fetched = _as(store, bob).get_view(BasicExperimentPage, view.id)
    assert fetched is not None
    assert fetched.id == view.id
    assert view.id in {v.id for v in _as(store, bob).list_views(experiment.id, bob.id)}


def test_a_stranger_cannot_see_a_shared_view_outside_the_project(store: SQLLiteStore) -> None:
    _bob, (_project, experiment, _run) = _shared_chain(store, ProjectRole.VIEWER)
    alice = store.find_user("alice")
    assert alice is not None
    stranger = _user(store, "stranger")
    view = _as(store, alice).create_view(
        BasicExperimentPage,
        models.NewPage[Any, Any](experiment_id=experiment.id, owner_id=alice.id, name="shared", shared=True),
    )

    assert _as(store, stranger).get_view(BasicExperimentPage, view.id) is None


# -- Artifact linking -------------------------------------------------------------------------


def _artifact_store(inner: AuthorizingDataStore, tmp_path: Path) -> AuthorizingArtifactStore:
    blob_store = BlobArtifactStore.get_or_create(FSBlobs(tmp_path), 10)
    return AuthorizingArtifactStore(blob_store, inner)


def test_link_artifacts_refuses_a_ref_already_attached_to_another_artifact(
    store: SQLLiteStore, tmp_path: Path
) -> None:
    """
    Regression: linking only checked that the caller could write to the *target* run, not whether
    the ref itself already belonged to someone else's artifact -- letting anyone who knew (or
    guessed) another artifact's ref link it into a run of their own, then read it back through
    their own, now-authorized, artifact id. `enforce=False` below isolates exactly that: with every
    *other* check (project role) turned off, this is the one thing left refusing the link.
    """
    chain = create_entity_chain(store, artifact=True)
    existing_ref = next(iter(store.fetch_artifacts(experiment_id=chain.experiment_id))).ref
    other_run = store.create_run(models.NewRun(experiment_id=chain.experiment_id))
    mallory = _user(store, "mallory")
    artifact_store = _artifact_store(_as(store, mallory, enforce=False), tmp_path)
    stolen = models.NewArtifact(
        key="stolen", fname="x.png", run_id=other_run.id, experiment_id=chain.experiment_id, step=0
    )

    with pytest.raises(models.UnservableArtifactRefError):
        artifact_store.link_artifacts([(stolen, AnyUrl(existing_ref))])


def test_link_artifacts_still_accepts_a_fresh_ref(store: SQLLiteStore, tmp_path: Path) -> None:
    chain = create_entity_chain(store)
    mallory = _user(store, "mallory")
    artifact_store = _artifact_store(_as(store, mallory, enforce=False), tmp_path)
    blob_backend = FSBlobs(tmp_path)
    ref = blob_backend.ref_for(blob_key(chain.experiment_id, chain.run_id, "img", 0, "a.png"))
    blob_backend.write(_staged(tmp_path, b"data"), ref)
    new_artifact = models.NewArtifact(
        key="img", fname="a.png", run_id=chain.run_id, experiment_id=chain.experiment_id, step=0
    )

    (linked,) = artifact_store.link_artifacts([(new_artifact, ref)])

    assert linked.ref == str(ref)


def _staged(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "staged"
    path.write_bytes(content)
    return path
