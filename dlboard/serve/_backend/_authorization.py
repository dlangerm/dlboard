"""
Authorization: the `DataStore`/`ArtifactStore` as one specific user is allowed to use it.

`get_data_store()` hands every request one of these, bound to whoever the request gate
authenticated -- so access control is structural, not a convention every page, callback, and
REST handler (or every storage backend) has to remember. A backend stays a dumb store with no idea
who's asking.

Every `DataStore` method is overridden explicitly -- there's deliberately no `__getattr__`
fallthrough, and `authorization_test.py` fails if a protocol method is ever added without a
decision here about who may call it.

The rules, in short (see `models.ProjectRole`): reading anything in a project needs `VIEWER`,
writing to it needs `EDITOR`, managing its members needs `OWNER`. A single entity the caller can't
see reads as missing (`None`), exactly like one that doesn't exist; a write it isn't allowed raises
`PermissionError` (a 403 over REST). Site-wide `Scope`s still gate what they always did (restore,
purge, the audit log), plus user management.

Under a provider that doesn't verify identity (see `AuthProvider.verifies_identity`) grants
aren't enforced at all -- every caller is an `EDITOR` everywhere, which is exactly the
local single-user experience -- and deletes stay gated by scope alone, as they always were.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn, Self

from dlboard import models
from dlboard.models import ProjectRole, Scope, has_scope, require_scope

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Iterator

    from flask import Response
    from pydantic import AnyUrl
    from werkzeug.datastructures import FileStorage

    from dlboard.serve._backend._metric_frame import MetricFrame, MetricKeySummary


def _forbid(what: str) -> NoReturn:
    msg = f"Not allowed: {what}"
    raise PermissionError(msg)


class AuthorizingDataStore(models.DataStore[...]):
    """A `DataStore` that only lets `actor` do what their project roles and scopes allow."""

    def __init__(
        self,
        inner: models.DataStore[...],
        actor: models.User,
        *,
        enforce_grants: bool,
        new_project_access: ProjectRole | None = None,
    ) -> None:
        self._inner = inner
        self._actor = actor
        self._enforce = enforce_grants
        self._new_project_access = new_project_access
        # Loaded lazily, once per request (one of these never outlives the request it was made for).
        self._projects: dict[int, models.Project] | None = None
        self._granted: dict[int, ProjectRole] | None = None
        self._experiment_projects: dict[int, int | None] = {}

    @classmethod
    def get_or_create(cls, *_args: object, **_kwargs: object) -> Self:
        """Never built this way -- `get_data_store()` builds one per request, around the real store."""
        msg = f"{cls.__name__} is built per request by get_data_store()"
        raise TypeError(msg)

    @property
    def backend_name(self) -> str:
        """The wrapped store's class name, for display (e.g. the admin page's About tab)."""
        return self._inner.__class__.__name__

    @property
    def actor(self) -> models.User:
        """Who this store is acting as."""
        return self._actor

    # -- Resolving roles ----------------------------------------------------------------------------

    def _all_projects(self) -> dict[int, models.Project]:
        if self._projects is None:
            self._projects = {p.id: p for p in self._inner.get_projects()}
        return self._projects

    def _granted_roles(self) -> dict[int, ProjectRole]:
        if self._granted is None:
            self._granted = {}
            for grant in self._inner.grants_for(self._actor):
                held = self._granted.get(grant.project_id)
                if held is None or grant.role.includes(held):
                    self._granted[grant.project_id] = grant.role
        return self._granted

    def role_on(self, project_id: int | None) -> ProjectRole | None:
        """The actor's effective role on `project_id`, or `None` if they can't see it at all."""
        if not self._enforce:
            return ProjectRole.EDITOR
        if project_id is None:
            return None
        if has_scope(self._actor, Scope.ALL):
            return ProjectRole.OWNER
        project = self._all_projects().get(project_id)
        roles = [r for r in (self._granted_roles().get(project_id), project and project.everyone_role) if r]
        return max(roles, key=list(ProjectRole).index, default=None)

    def _can(self, project_id: int | None, role: ProjectRole) -> bool:
        held = self.role_on(project_id)
        return held is not None and held.includes(role)

    def _require(self, project_id: int | None, role: ProjectRole) -> None:
        if not self._can(project_id, role):
            msg = f"User {self._actor.id} needs the {role} role on project {project_id}"
            raise PermissionError(msg)

    def _require_scope_or_role(self, scope: Scope, project_id: int | None, role: ProjectRole) -> None:
        """Deletes: the site-wide scope always suffices; a project role only does when grants are enforced."""
        if not (has_scope(self._actor, scope) or (self._enforce and self._can(project_id, role))):
            require_scope(self._actor, scope)

    def _bound(self, actor: models.User) -> models.User:
        """The actor this store is bound to -- refusing a call that claims to act as anyone else."""
        if actor.id != self._actor.id:
            msg = f"User {self._actor.id} cannot act as user {actor.id}"
            raise PermissionError(msg)
        return self._actor

    def _require_self_or(self, user_id: int | None, scope: Scope) -> None:
        if user_id != self._actor.id:
            require_scope(self._actor, scope)

    def _experiment_project(self, experiment_id: int) -> int | None:
        if experiment_id not in self._experiment_projects:
            experiment = self._inner.get_experiment(experiment_id)
            self._experiment_projects[experiment_id] = experiment.project_id if experiment else None
        return self._experiment_projects[experiment_id]

    def _page_project(
        self, *, run_id: int | None, experiment_id: int | None, project_id: int | None
    ) -> int | None:
        if run_id is not None:
            run = self._inner.get_run(run_id)
            return self._experiment_project(run.experiment_id) if run else None
        if experiment_id is not None:
            return self._experiment_project(experiment_id)
        return project_id

    def require_run_write(self, run_id: int, experiment_id: int) -> None:
        """Raise unless the actor may log data to `run_id`, which must really belong to `experiment_id`."""
        run = self._inner.get_run(run_id)
        if self._enforce and (run is None or run.experiment_id != experiment_id):
            msg = f"Run {run_id} does not belong to experiment {experiment_id}"
            raise PermissionError(msg)
        self._require(self._experiment_project(experiment_id), ProjectRole.EDITOR)

    def _require_runs_write(self, rows: Iterable[models.LoggedMetrics | models.Artifact]) -> None:
        for run_id, experiment_id in {(r.run_id, r.experiment_id) for r in rows}:
            self.require_run_write(run_id, experiment_id)

    # -- Users and credentials ------------------------------------------------------------------------

    def get_or_create_user(self, principal: models.Principal) -> models.User:
        require_scope(self._actor, Scope.USER_MANAGE)
        return self._inner.get_or_create_user(principal)

    def get_user(self, user_id: int) -> models.User | None:
        return next((u for u in self._visible_users() if u.id == user_id), None)

    def find_user(self, username: str) -> models.User | None:
        return self._inner.find_user(username)

    def update_user(self, user: models.User) -> models.User:
        """
        Update a user. `Scope.USER_MANAGE` alone is never enough to touch `Scope.ALL`.

        Granting `ALL` to anyone (including yourself) or changing an existing `ALL` admin's record
        at all -- scopes, disabling them, anything -- needs `ALL` itself. Otherwise `USER_MANAGE`
        is a strictly weaker scope than `ALL` in name only: its holder could self-escalate to full
        admin, or lock out a real one, with no `ALL` of their own.
        """
        require_scope(self._actor, Scope.USER_MANAGE)
        existing = self._inner.get_user(user.id)
        touches_all_admin = Scope.ALL in user.scopes or (
            existing is not None and Scope.ALL in existing.scopes
        )
        if touches_all_admin:
            require_scope(self._actor, Scope.ALL)
        return self._inner.update_user(user)

    def list_users(self) -> list[models.User]:
        return list(self._visible_users())

    def _visible_users(self) -> Iterator[models.User]:
        """Everyone, for an admin; otherwise the actor and the members of projects they can see."""
        if not self._enforce or has_scope(self._actor, Scope.USER_MANAGE):
            yield from self._inner.list_users()
            return
        visible = frozenset(self._visible_project_ids())
        projects = self._all_projects()
        known: set[int | None] = {self._actor.id}
        known.update(g.user_id for g in self._inner.list_project_grants(visible))
        known.update(projects[pid].created_by for pid in visible)
        yield from (u for u in self._inner.list_users() if u.id in known)

    def create_api_token(self, token: models.NewApiToken) -> models.ApiToken:
        self._require_self_or(token.user_id, Scope.USER_MANAGE)
        return self._inner.create_api_token(token)

    def get_api_token(self, token_id: str) -> models.ApiToken | None:  # noqa: ARG002
        _forbid("API tokens are only looked up by the request gate")

    def list_api_tokens(self, user_id: int) -> list[models.ApiToken]:
        self._require_self_or(user_id, Scope.USER_MANAGE)
        return self._inner.list_api_tokens(user_id)

    def revoke_api_token(self, api_token_id: int, user_id: int) -> None:
        self._require_self_or(user_id, Scope.USER_MANAGE)
        self._inner.revoke_api_token(api_token_id, user_id)

    def touch_api_token(self, api_token_id: int) -> None:  # noqa: ARG002
        _forbid("API tokens are only touched by the request gate")

    def get_password_credential(self, user_id: int) -> models.PasswordCredential | None:  # noqa: ARG002
        _forbid("password hashes are only read by the password provider")

    def set_password_credential(self, credential: models.PasswordCredential) -> None:
        self._require_self_or(credential.user_id, Scope.USER_MANAGE)
        self._inner.set_password_credential(credential)

    # -- Projects and their grants ----------------------------------------------------------------

    def _visible_project_ids(self) -> Iterator[int]:
        return (pid for pid in self._all_projects() if self._can(pid, ProjectRole.VIEWER))

    def list_project_grants(self, project_ids: Collection[int]) -> list[models.ProjectGrant]:
        return self._inner.list_project_grants([p for p in project_ids if self._can(p, ProjectRole.VIEWER)])

    def grants_for(self, user: models.User) -> list[models.ProjectGrant]:
        self._require_self_or(user.id, Scope.USER_MANAGE)
        return self._inner.grants_for(user)

    def set_project_grant(self, grant: models.NewProjectGrant) -> models.ProjectGrant:
        self._require(grant.project_id, ProjectRole.OWNER)
        return self._inner.set_project_grant(grant)

    def delete_project_grant(self, project_id: int, grant_id: int) -> None:
        self._require(project_id, ProjectRole.OWNER)
        self._inner.delete_project_grant(project_id, grant_id)

    def create_project(self, project: models.NewProject) -> models.Project:
        """Create a project owned by the actor, starting at the deployment's default `everyone_role`."""
        if project.everyone_role is None and self._new_project_access is not None:
            project = project.model_copy(update={"everyone_role": self._new_project_access})
        created = self._inner.create_project(project.model_copy(update={"created_by": self._actor.id}))
        self._inner.set_project_grant(
            models.NewProjectGrant(project_id=created.id, user_id=self._actor.id, role=ProjectRole.OWNER)
        )
        self._projects, self._granted = None, None
        return created

    def get_or_create_project(
        self,
        name: str,
        description: str = "",
        created_by: int | None = None,  # noqa: ARG002 -- always created by (and owned by) the actor
    ) -> models.Project:
        """
        The project named `name` the actor can write to, creating one if there's none.

        Scoped to the actor's own writable projects, so two people both logging to "mnist" each
        get their own rather than one being refused the other's. Raises `AmbiguousProjectError` if
        the actor can write to more than one project of that name.
        """
        writable = (p for p in self.get_projects() if p.name == name and self._can(p.id, ProjectRole.EDITOR))
        # Two is all it takes to know the name is ambiguous, so never look past the second match.
        match next(writable, None), next(writable, None):
            case None, _:
                return self.create_project(models.NewProject(name=name, description=description))
            case project, None:
                return project
            case _:
                msg = f"You can write to more than one project named {name!r}; refer to one by id instead"
                raise models.AmbiguousProjectError(msg)

    def get_project(self, database_id: int) -> models.Project | None:
        return self._inner.get_project(database_id) if self._can(database_id, ProjectRole.VIEWER) else None

    def get_projects(self) -> Iterator[models.Project]:
        projects = self._all_projects()
        return (projects[pid] for pid in self._visible_project_ids())

    def get_project_stats(self) -> dict[int, models.ProjectStats]:
        stats = self._inner.get_project_stats()
        return {pid: stats[pid] for pid in self._visible_project_ids() if pid in stats}

    def update_project(self, project: models.Project) -> models.Project:
        """Rename/redescribe a project (`EDITOR`); changing who it's shared with needs `OWNER`."""
        self._require(project.id, ProjectRole.EDITOR)
        current = self._all_projects().get(project.id)
        if current is not None and current.everyone_role != project.everyone_role:
            self._require(project.id, ProjectRole.OWNER)
        return self._inner.update_project(project)

    # -- Experiments, runs, and everything logged to them -------------------------------------------

    def create_experiment(self, experiment: models.NewExperiment) -> models.Experiment:
        self._require(experiment.project_id, ProjectRole.EDITOR)
        return self._inner.create_experiment(experiment)

    def get_or_create_experiment(
        self,
        project_id: int,
        name: str = "default",
        created_by: int | None = None,
        source: models.ExperimentSource | None = None,
    ) -> models.Experiment:
        self._require(project_id, ProjectRole.EDITOR)
        return self._inner.get_or_create_experiment(project_id, name, created_by, source)

    def get_experiment(self, database_id: int) -> models.Experiment | None:
        experiment = self._inner.get_experiment(database_id)
        return experiment if experiment and self._can(experiment.project_id, ProjectRole.VIEWER) else None

    def get_experiments(self, project_id: int) -> Iterator[models.Experiment]:
        self._require(project_id, ProjectRole.VIEWER)
        return self._inner.get_experiments(project_id)

    def get_experiment_stats(self, project_id: int) -> dict[int, models.ActivityStats]:
        self._require(project_id, ProjectRole.VIEWER)
        return self._inner.get_experiment_stats(project_id)

    def update_experiment(self, experiment: models.Experiment) -> models.Experiment:
        self._require(self._experiment_project(experiment.id), ProjectRole.EDITOR)
        self._require(experiment.project_id, ProjectRole.EDITOR)
        return self._inner.update_experiment(experiment)

    def create_run(self, run: models.NewRun) -> models.Run:
        self._require(self._experiment_project(run.experiment_id), ProjectRole.EDITOR)
        return self._inner.create_run(run)

    def get_run(self, run_id: int) -> models.Run | None:
        run = self._inner.get_run(run_id)
        return (
            run
            if run and self._can(self._experiment_project(run.experiment_id), ProjectRole.VIEWER)
            else None
        )

    def get_runs(self, experiment_id: int, *, limit: int = 1000, offset: int = 0) -> Iterator[models.Run]:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.get_runs(experiment_id, limit=limit, offset=offset)

    def log_metrics(self, metric: Iterable[models.LoggedMetrics]) -> None:
        metric = list(metric)
        self._require_runs_write(metric)
        self._inner.log_metrics(metric)

    def fetch_metrics(
        self,
        experiment_id: int,
        *,
        keys: frozenset[str] | None = None,
        exclude_run_ids: frozenset[int] = frozenset(),
    ) -> MetricFrame:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.fetch_metrics(experiment_id, keys=keys, exclude_run_ids=exclude_run_ids)

    def summarize_metric_keys(self, experiment_id: int) -> list[MetricKeySummary]:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.summarize_metric_keys(experiment_id)

    def log_hyperparams(self, hyperparams: models.NewHyperParams) -> models.HyperParams:
        self.require_run_write(hyperparams.run_id, hyperparams.experiment_id)
        return self._inner.log_hyperparams(hyperparams)

    def fetch_hyperparams(
        self, experiment_id: int, *, exclude_run_ids: frozenset[int] = frozenset()
    ) -> Iterator[models.HyperParams]:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.fetch_hyperparams(experiment_id, exclude_run_ids=exclude_run_ids)

    def log_artifact_refs(self, artifacts: Iterable[models.Artifact]) -> None:
        artifacts = list(artifacts)
        self._require_runs_write(artifacts)
        self._inner.log_artifact_refs(artifacts)

    def count_artifacts_by_ref(self, ref: str) -> int:
        """A count, not row contents -- used to decide *whether* a link is safe, not to read anything."""
        return self._inner.count_artifacts_by_ref(ref)

    def get_artifact(self, artifact_id: int) -> models.Artifact | None:
        artifact = self._inner.get_artifact(artifact_id)
        visible = artifact and self._can(self._experiment_project(artifact.experiment_id), ProjectRole.VIEWER)
        return artifact if visible else None

    def fetch_artifacts(
        self,
        experiment_id: int,
        *,
        keys: frozenset[str] | None = None,
        exclude_run_ids: frozenset[int] = frozenset(),
    ) -> Iterator[models.Artifact]:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.fetch_artifacts(experiment_id, keys=keys, exclude_run_ids=exclude_run_ids)

    # -- Pages, views, and notes --------------------------------------------------------------------

    def get_or_create_page[Dataframe, Panel, Chart](
        self,
        page_type: type[models.Page[Dataframe, Panel, Chart]],
        *,
        run_id: int | None = None,
        experiment_id: int | None = None,
        project_id: int | None = None,
        new_page_type: type[models.NewPage[Dataframe, Chart]] | None = None,
    ) -> models.Page[Dataframe, Panel, Chart]:
        owner = self._page_project(run_id=run_id, experiment_id=experiment_id, project_id=project_id)
        self._require(owner, ProjectRole.VIEWER)
        return self._inner.get_or_create_page(
            page_type,
            run_id=run_id,
            experiment_id=experiment_id,
            project_id=project_id,
            new_page_type=new_page_type,
        )

    def update_page[Dataframe, Panel, Chart](
        self, page: models.Page[Dataframe, Panel, Chart]
    ) -> models.Page[Dataframe, Panel, Chart]:
        """
        Write a page: one's own view freely, the shared page's layout only as an `EDITOR`.

        Shared viewing state (`page_settings` alone -- e.g. which panel is open, see
        `persist_settings`'s `branch_on_edit`) is last-write-wins for anyone who can see the page.
        """
        project_id = self._page_project(
            run_id=page.run_id, experiment_id=page.experiment_id, project_id=page.project_id
        )
        if page.owner_id is not None:
            stored = self._inner.get_view(type(page), page.id)
            if stored is None or stored.owner_id != self._actor.id or page.owner_id != self._actor.id:
                _forbid("only a view's owner can change it")
            self._require(project_id, ProjectRole.VIEWER)
            return self._inner.update_page(page)
        shared = self._inner.get_or_create_page(
            type(page), run_id=page.run_id, experiment_id=page.experiment_id, project_id=page.project_id
        )
        layout_unchanged = shared.model_dump(exclude={"page_settings"}) == page.model_dump(
            exclude={"page_settings"}
        )
        self._require(project_id, ProjectRole.VIEWER if layout_unchanged else ProjectRole.EDITOR)
        return self._inner.update_page(page)

    def create_view[Dataframe, Panel, Chart](
        self, page_type: type[models.Page[Dataframe, Panel, Chart]], view: models.NewPage[Dataframe, Chart]
    ) -> models.Page[Dataframe, Panel, Chart]:
        if view.owner_id != self._actor.id:
            _forbid("a view can only be saved as its own owner")
        owner = self._page_project(
            run_id=view.run_id, experiment_id=view.experiment_id, project_id=view.project_id
        )
        self._require(owner, ProjectRole.VIEWER)
        return self._inner.create_view(page_type, view)

    def get_view[Dataframe, Panel, Chart](
        self, page_type: type[models.Page[Dataframe, Panel, Chart]], view_id: int
    ) -> models.Page[Dataframe, Panel, Chart] | None:
        """A view by id, if `self._actor` can see it: its owner, always; anyone else only if it's shared."""
        view = self._inner.get_view(page_type, view_id)
        if view is None:
            return None
        if self._enforce and view.owner_id != self._actor.id and not view.shared:
            return None
        owner = self._page_project(
            run_id=view.run_id, experiment_id=view.experiment_id, project_id=view.project_id
        )
        return view if self._can(owner, ProjectRole.VIEWER) else None

    def list_views(self, experiment_id: int, viewer_id: int) -> list[models.ViewSummary]:
        self._require_self_or(viewer_id, Scope.ALL)
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.list_views(experiment_id, viewer_id)

    def delete_view(self, view_id: int, owner_id: int) -> None:
        self._require_self_or(owner_id, Scope.ALL)
        self._inner.delete_view(view_id, owner_id)

    def add_comment(self, comment: models.NewComment) -> models.Comment:
        if comment.author_id != self._actor.id:
            _forbid("a note can only be posted as its own author")
        self._require(self._experiment_project(comment.experiment_id), ProjectRole.VIEWER)
        return self._inner.add_comment(comment)

    def list_comments(self, experiment_id: int) -> list[models.Comment]:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.list_comments(experiment_id)

    def delete_comment(self, comment_id: int, author_id: int) -> None:
        self._require_self_or(author_id, Scope.ALL)
        self._inner.delete_comment(comment_id, author_id)

    # -- Soft-delete, restore, purge ------------------------------------------------------------------
    #
    # The `actor` every one of these takes must be the one this store is bound to (see `_bound`) --
    # a caller can't act as anyone but who the request gate authenticated.

    def _artifact_project(self, artifact_id: int) -> int | None:
        artifact = self._inner.get_artifact(artifact_id)
        return self._experiment_project(artifact.experiment_id) if artifact else None

    def _run_project(self, run_id: int) -> int | None:
        run = self._inner.get_run(run_id)
        return self._experiment_project(run.experiment_id) if run else None

    def delete_project(self, project_id: int, actor: models.User) -> None:
        self._require_scope_or_role(Scope.PROJECT_DELETE, project_id, ProjectRole.OWNER)
        self._inner.delete_project(project_id, self._bound(actor))

    def restore_project(self, project_id: int, actor: models.User) -> None:
        require_scope(self._actor, Scope.RESTORE)
        self._inner.restore_project(project_id, self._bound(actor))

    def purge_project(self, project_id: int, actor: models.User) -> None:
        require_scope(self._actor, Scope.PURGE)
        self._inner.purge_project(project_id, self._bound(actor))

    def delete_experiment(self, experiment_id: int, actor: models.User) -> None:
        self._require_scope_or_role(
            Scope.EXPERIMENT_DELETE, self._experiment_project(experiment_id), ProjectRole.EDITOR
        )
        self._inner.delete_experiment(experiment_id, self._bound(actor))

    def restore_experiment(self, experiment_id: int, actor: models.User) -> None:
        require_scope(self._actor, Scope.RESTORE)
        self._inner.restore_experiment(experiment_id, self._bound(actor))

    def purge_experiment(self, experiment_id: int, actor: models.User) -> None:
        require_scope(self._actor, Scope.PURGE)
        self._inner.purge_experiment(experiment_id, self._bound(actor))

    def delete_run(self, run_id: int, actor: models.User) -> None:
        self._require_scope_or_role(Scope.RUN_DELETE, self._run_project(run_id), ProjectRole.EDITOR)
        self._inner.delete_run(run_id, self._bound(actor))

    def restore_run(self, run_id: int, actor: models.User) -> None:
        require_scope(self._actor, Scope.RESTORE)
        self._inner.restore_run(run_id, self._bound(actor))

    def purge_run(self, run_id: int, actor: models.User) -> None:
        require_scope(self._actor, Scope.PURGE)
        self._inner.purge_run(run_id, self._bound(actor))

    def delete_artifact(self, artifact_id: int, actor: models.User) -> None:
        self._require_scope_or_role(
            Scope.ARTIFACT_DELETE, self._artifact_project(artifact_id), ProjectRole.EDITOR
        )
        self._inner.delete_artifact(artifact_id, self._bound(actor))

    def restore_artifact(self, artifact_id: int, actor: models.User) -> None:
        require_scope(self._actor, Scope.RESTORE)
        self._inner.restore_artifact(artifact_id, self._bound(actor))

    def purge_artifact(self, artifact_id: int, actor: models.User) -> None:
        require_scope(self._actor, Scope.PURGE)
        self._inner.purge_artifact(artifact_id, self._bound(actor))

    # The trash spans every project, so under enforced grants it's an admin-only view.

    def _require_trash_access(self) -> None:
        if self._enforce:
            require_scope(self._actor, Scope.RESTORE)

    def list_deleted_projects(self, limit: int = 100, offset: int = 0) -> Iterator[models.Project]:
        self._require_trash_access()
        return self._inner.list_deleted_projects(limit, offset)

    def list_deleted_experiments(self, limit: int = 100, offset: int = 0) -> Iterator[models.Experiment]:
        self._require_trash_access()
        return self._inner.list_deleted_experiments(limit, offset)

    def list_deleted_runs(self, limit: int = 100, offset: int = 0) -> Iterator[models.Run]:
        self._require_trash_access()
        return self._inner.list_deleted_runs(limit, offset)

    def list_deleted_artifacts(self, limit: int = 100, offset: int = 0) -> Iterator[models.Artifact]:
        self._require_trash_access()
        return self._inner.list_deleted_artifacts(limit, offset)

    def list_audit_log(
        self, actor: models.User, limit: int = 100, offset: int = 0
    ) -> Iterator[models.AuditLogEntry]:
        require_scope(self._actor, Scope.AUDIT_LOG_READ)
        return self._inner.list_audit_log(self._bound(actor), limit=limit, offset=offset)

    def list_pending_artifact_purges(
        self, limit: int = 100, offset: int = 0
    ) -> Iterator[models.ArtifactPurgeTask]:
        require_scope(self._actor, Scope.PURGE)
        return self._inner.list_pending_artifact_purges(limit, offset)

    def count_pending_artifact_purges(self) -> int:
        return self._inner.count_pending_artifact_purges()

    def complete_artifact_purge(self, task_id: int) -> None:
        require_scope(self._actor, Scope.PURGE)
        self._inner.complete_artifact_purge(task_id)

    def fail_artifact_purge(self, task_id: int, error: str) -> None:
        require_scope(self._actor, Scope.PURGE)
        self._inner.fail_artifact_purge(task_id, error)


class AuthorizingArtifactStore(models.ArtifactStore[...]):
    """An `ArtifactStore` that only accepts artifacts for runs its `authorizer`'s actor may write to."""

    def __init__(self, inner: models.ArtifactStore[...], authorizer: AuthorizingDataStore) -> None:
        self._inner = inner
        self._authorizer = authorizer

    @classmethod
    def get_or_create(cls, *_args: object, **_kwargs: object) -> Self:
        """Never built this way -- `get_artifact_store()` builds one per request, around the real store."""
        msg = f"{cls.__name__} is built per request by get_artifact_store()"
        raise TypeError(msg)

    def log_artifacts(self, artifacts: Iterable[tuple[models.NewArtifact, FileStorage]]) -> None:
        artifacts = list(artifacts)
        for run_id, experiment_id in {(a.run_id, a.experiment_id) for a, _ in artifacts}:
            self._authorizer.require_run_write(run_id, experiment_id)
        self._inner.log_artifacts(artifacts)

    def link_artifacts(self, links: Iterable[tuple[models.NewArtifact, AnyUrl]]) -> list[models.Artifact]:
        """
        Register a batch of already-stored artifacts by ref, with no bytes moved.

        Refusing a ref already attached to an existing artifact (regardless of who can see it) is
        what keeps this from being a way to read -- or, on purge, silently orphan -- someone else's
        blob: knowing (or guessing) another project's ref would otherwise be enough to link it into
        a run you do control, and then read it back through your own, now-authorized, artifact id.
        """
        links = list(links)
        for run_id, experiment_id in {(a.run_id, a.experiment_id) for a, _ in links}:
            self._authorizer.require_run_write(run_id, experiment_id)
        already_linked = [ref for _, ref in links if self._authorizer.count_artifacts_by_ref(str(ref)) > 0]
        if already_linked:
            msg = f"refusing to link {len(already_linked)} ref(s) already attached to another artifact: {already_linked}"
            raise models.UnservableArtifactRefError(msg)
        return self._inner.link_artifacts(links)

    def download_artifact(self, ref: AnyUrl) -> Response:
        """Only ever reached with a ref read through an authorized `get_artifact` (see `_artifact_download`)."""
        return self._inner.download_artifact(ref)

    def delete_artifact(self, ref: AnyUrl) -> None:
        require_scope(self._authorizer.actor, Scope.PURGE)
        self._inner.delete_artifact(ref)
