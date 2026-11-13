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

from typing import TYPE_CHECKING, Any, NoReturn, Self

from dlboard import models
from dlboard.models import ProjectRole, Scope, has_scope, require_scope
from dlboard.serve._backend._setting_rules import SettingRule, SettingWrite

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
    from datetime import datetime

    from flask import Response
    from pydantic import AnyUrl
    from werkzeug.datastructures import FileStorage

    from dlboard.serve._backend._metric_frame import MetricFrame, MetricKeySummary
    from dlboard.serve._backend._run_guards import RunGuard


def _forbid(what: str) -> NoReturn:
    msg = f"Not allowed: {what}"
    raise PermissionError(msg)


class AuthorizingDataStore(models.DataStore[...]):
    """A `DataStore` that only lets `actor` do what their project roles and scopes allow."""

    def __init__(  # noqa: PLR0913 -- what a request's store is built from, each once
        self,
        inner: models.DataStore[...],
        actor: models.User,
        *,
        enforce_grants: bool,
        new_project_access: ProjectRole | None = None,
        setting_rules: Mapping[str, SettingRule] | None = None,
        run_guards: Sequence[RunGuard] = (),
    ) -> None:
        self._inner = inner
        self._actor = actor
        self._enforce = enforce_grants
        self._new_project_access = new_project_access
        self._setting_rules = setting_rules or {}
        self._run_guards = run_guards
        # Loaded lazily, once per request (one of these never outlives the request it was made for).
        self._projects: dict[int, models.Project] | None = None
        self._granted: dict[int, ProjectRole] | None = None
        self._experiment_projects: dict[int, int | None] = {}
        self._settings: dict[tuple[str, int], models.ScopedSettings] = {}

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
        everyone_role = project.everyone_role if project else None
        roles = [r for r in (self._granted_roles().get(project_id), everyone_role) if r]
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
        return next(iter(self.get_users([user_id])), None)

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

    def get_users(
        self, user_ids: Collection[int] | None = None, *, name_contains: str = "", limit: int | None = None
    ) -> list[models.User]:
        """Everyone, for an admin; otherwise the actor and the members and creators of the projects they can see."""
        if self._enforce and not has_scope(self._actor, Scope.USER_MANAGE):
            user_ids = self._visible_user_ids(user_ids)
        return self._inner.get_users(user_ids, name_contains=name_contains, limit=limit)

    def _visible_user_ids(self, among: Collection[int] | None) -> frozenset[int]:
        """The users the actor may see (of `among`, if given): asked of the database for those, not for every user."""
        visible = frozenset(self._visible_project_ids())
        projects = self._all_projects()
        known: set[int | None] = {self._actor.id}
        known.update(g.user_id for g in self._inner.list_project_grants(visible, user_ids=among))
        known.update(projects[pid].created_by for pid in visible)
        return frozenset(
            user_id for user_id in known if user_id is not None and (among is None or user_id in among)
        )

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

    def list_project_grants(
        self, project_ids: Collection[int], *, user_ids: Collection[int] | None = None
    ) -> list[models.ProjectGrant]:
        return self._inner.list_project_grants(
            [p for p in project_ids if self._can(p, ProjectRole.VIEWER)], user_ids=user_ids
        )

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
            case models.Project() as project, None:
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

    def search_experiments(
        self, project_id: int, *, name_contains: str = "", limit: int = 24, offset: int = 0
    ) -> models.ExperimentList:
        self._require(project_id, ProjectRole.VIEWER)
        return self._inner.search_experiments(
            project_id, name_contains=name_contains, limit=limit, offset=offset
        )

    def search_experiments_in(
        self, project_ids: Collection[int], *, name_contains: str, limit: int
    ) -> list[models.Experiment]:
        """Of the projects asked for, only those the viewer sees are searched."""
        return self._inner.search_experiments_in(
            [p for p in project_ids if self._can(p, ProjectRole.VIEWER)],
            name_contains=name_contains,
            limit=limit,
        )

    def get_experiments_by_id(self, experiment_ids: Collection[int]) -> list[models.Experiment]:
        """Only the experiments of projects the viewer sees."""
        return [
            e
            for e in self._inner.get_experiments_by_id(experiment_ids)
            if self._can(e.project_id, ProjectRole.VIEWER)
        ]

    def get_experiment_stats(
        self, project_id: int, experiment_ids: Collection[int] | None = None
    ) -> dict[int, models.ActivityStats]:
        self._require(project_id, ProjectRole.VIEWER)
        return self._inner.get_experiment_stats(project_id, experiment_ids)

    def update_experiment(self, experiment: models.Experiment) -> models.Experiment:
        self._require(self._experiment_project(experiment.id), ProjectRole.EDITOR)
        self._require(experiment.project_id, ProjectRole.EDITOR)
        return self._inner.update_experiment(experiment)

    def create_run(self, run: models.NewRun) -> models.Run:
        project_id = self._experiment_project(run.experiment_id)
        self._require(project_id, ProjectRole.EDITOR)
        if project_id is not None:
            for guard in self._run_guards:
                guard(self._inner, project_id)
        return self._inner.create_run(run)

    def get_run(self, run_id: int) -> models.Run | None:
        run = self._inner.get_run(run_id)
        return (
            run
            if run and self._can(self._experiment_project(run.experiment_id), ProjectRole.VIEWER)
            else None
        )

    def finish_run(self, run_id: int, status: models.RunStatus, ended_at: datetime) -> models.Run:
        self._require(self._run_project(run_id), ProjectRole.EDITOR)
        return self._inner.finish_run(run_id, status, ended_at)

    def get_runs(  # noqa: PLR0913 -- one keyword per filter of a run list
        self,
        experiment_id: int,
        *,
        limit: int | None = 1000,
        offset: int = 0,
        run_ids: frozenset[int] | None,
        name_contains: str = "",
        created_before: datetime | None = None,
        oldest_first: bool = False,
        include_kept: bool = True,
    ) -> Iterator[models.Run]:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.get_runs(
            experiment_id,
            limit=limit,
            offset=offset,
            run_ids=run_ids,
            name_contains=name_contains,
            created_before=created_before,
            oldest_first=oldest_first,
            include_kept=include_kept,
        )

    def count_runs(
        self,
        experiment_id: int,
        *,
        before: tuple[datetime, int] | None = None,
        include_kept: bool = True,
        spare_running_after: datetime | None = None,
        limit: int,
    ) -> int:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.count_runs(
            experiment_id,
            before=before,
            include_kept=include_kept,
            spare_running_after=spare_running_after,
            limit=limit,
        )

    def log_metrics(self, metric: Iterable[models.LoggedMetrics]) -> None:
        metric = list(metric)
        self._require_runs_write(metric)
        self._inner.log_metrics(metric)

    def fetch_metrics(
        self,
        experiment_id: int,
        *,
        keys: frozenset[str] | None = None,
        run_ids: frozenset[int] | None,
        reduction: models.MetricReduction = models.MetricReduction.HISTORY,
    ) -> MetricFrame:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.fetch_metrics(experiment_id, keys=keys, run_ids=run_ids, reduction=reduction)

    def summarize_metric_keys(self, experiment_id: int) -> list[str]:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.summarize_metric_keys(experiment_id)

    def summarize_metric_steps(self, experiment_id: int, keys: frozenset[str]) -> list[MetricKeySummary]:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.summarize_metric_steps(experiment_id, keys)

    def fetch_series_band(
        self,
        experiment_id: int,
        *,
        groups: Mapping[str, str],
        run_ids: frozenset[int] | None,
        grid: int,
    ) -> list[models.SeriesBand]:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.fetch_series_band(experiment_id, groups=groups, run_ids=run_ids, grid=grid)

    def fetch_histograms(
        self,
        experiment_id: int,
        *,
        keys: frozenset[str],
        max_steps: int,
        run_ids: frozenset[int] | None,
    ) -> list[models.UnderlyingHistogramTableEntry]:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.fetch_histograms(experiment_id, keys=keys, max_steps=max_steps, run_ids=run_ids)

    def summarize_histogram_keys(self, experiment_id: int) -> list[str]:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.summarize_histogram_keys(experiment_id)

    def log_hyperparams(self, hyperparams: models.NewHyperParams) -> models.HyperParams:
        self.require_run_write(hyperparams.run_id, hyperparams.experiment_id)
        return self._inner.log_hyperparams(hyperparams)

    def fetch_hyperparams(
        self, experiment_id: int, *, run_ids: frozenset[int] | None
    ) -> Iterator[models.HyperParams]:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.fetch_hyperparams(experiment_id, run_ids=run_ids)

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
        key_prefixes: frozenset[str] = frozenset(),
        run_ids: frozenset[int] | None,
        steps: models.StepRange | frozenset[int] | None = None,
    ) -> Iterator[models.Artifact]:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.fetch_artifacts(
            experiment_id, keys=keys, key_prefixes=key_prefixes, run_ids=run_ids, steps=steps
        )

    def artifact_steps(
        self, experiment_id: int, *, key: str, run_ids: frozenset[int] | None
    ) -> dict[int, list[int]]:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.artifact_steps(experiment_id, key=key, run_ids=run_ids)

    def summarize_artifact_keys(self, experiment_id: int) -> list[models.Artifact]:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.summarize_artifact_keys(experiment_id)

    # -- Pages, views, and notes --------------------------------------------------------------------

    def get_or_create_page[Dataframe, Chart](
        self,
        page_type: type[models.Page[Dataframe, Chart]],
        *,
        run_id: int | None = None,
        experiment_id: int | None = None,
        project_id: int | None = None,
        new_page_type: type[models.NewPage[Dataframe, Chart]] | None = None,
    ) -> models.Page[Dataframe, Chart]:
        owner = self._page_project(run_id=run_id, experiment_id=experiment_id, project_id=project_id)
        self._require(owner, ProjectRole.VIEWER)
        return self._inner.get_or_create_page(
            page_type,
            run_id=run_id,
            experiment_id=experiment_id,
            project_id=project_id,
            new_page_type=new_page_type,
        )

    def update_page[Dataframe, Chart](
        self, page: models.Page[Dataframe, Chart]
    ) -> models.Page[Dataframe, Chart]:
        """
        Write a page: one's own view freely, the shared page only as an `EDITOR`.

        Nothing about how a page is being browsed (which panel is open, which tab) is stored on it, so
        a `VIEWER` has nothing of the shared page's to change -- not its panels, and not its settings
        (which runs it leaves out, say), which are as much what everyone sees as its panels are.
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
        self._require(project_id, ProjectRole.EDITOR)
        return self._inner.update_page(page)

    def create_view[Dataframe, Chart](
        self, page_type: type[models.Page[Dataframe, Chart]], view: models.NewPage[Dataframe, Chart]
    ) -> models.Page[Dataframe, Chart]:
        if view.owner_id != self._actor.id:
            _forbid("a view can only be saved as its own owner")
        owner = self._page_project(
            run_id=view.run_id, experiment_id=view.experiment_id, project_id=view.project_id
        )
        self._require(owner, ProjectRole.VIEWER)
        return self._inner.create_view(page_type, view)

    def get_view[Dataframe, Chart](
        self, page_type: type[models.Page[Dataframe, Chart]], view_id: int
    ) -> models.Page[Dataframe, Chart] | None:
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

    # -- Comparisons ---------------------------------------------------------------------------------
    #
    # A comparison is a view scoped to a project, so it is seen as one is: by its owner, and by anyone who can see
    # the project if it is shared. Its runs are the one thing a shared one lets other people change: any editor of
    # the project may add and remove them, while its charts and settings stay its owner's (an edit forks a copy).

    def _visible_comparison(self, page_id: int) -> models.Page[Any, Any]:
        page = self.get_view(models.Page[Any, Any], page_id)
        if page is None or page.project_id is None:
            _forbid("there is no such comparison, or it is not yours to see")
        return page

    def _may_change_runs(self, owner_id: int, project_id: int | None, *, shared: bool) -> bool:
        """The one rule of who changes a comparison's runs: its owner, or an editor of the project if it is shared."""
        return owner_id == self._actor.id or (shared and self._can(project_id, ProjectRole.EDITOR))

    def _editable_comparison(self, page_id: int) -> models.Page[Any, Any]:
        page = self._visible_comparison(page_id)
        if page.owner_id is None or not self._may_change_runs(
            page.owner_id, page.project_id, shared=page.shared
        ):
            _forbid("only a comparison's owner, or an editor of a shared one, can change its runs")
        return page

    def create_comparison[Dataframe, Chart](
        self,
        page_type: type[models.Page[Dataframe, Chart]],
        comparison: models.NewPage[Dataframe, Chart],
        run_ids: frozenset[int],
    ) -> models.Page[Dataframe, Chart]:
        if comparison.owner_id != self._actor.id:
            _forbid("a comparison can only be saved as its own owner")
        self._require(comparison.project_id, ProjectRole.VIEWER)
        return self._inner.create_comparison(page_type, comparison, run_ids)

    def comparison_runs(self, page_id: int) -> list[models.Run]:
        self._visible_comparison(page_id)
        return self._inner.comparison_runs(page_id)

    def add_comparison_runs(self, page_id: int, run_ids: frozenset[int], *, added_by: int) -> int:
        if added_by != self._actor.id:
            _forbid("a run can only be added to a comparison as yourself")
        self._editable_comparison(page_id)
        return self._inner.add_comparison_runs(page_id, run_ids, added_by=added_by)

    def remove_comparison_runs(self, page_id: int, run_ids: frozenset[int]) -> None:
        self._editable_comparison(page_id)
        self._inner.remove_comparison_runs(page_id, run_ids)

    def list_comparisons(self, project_id: int, viewer_id: int) -> list[models.ComparisonSummary]:
        self._require_self_or(viewer_id, Scope.ALL)
        self._require(project_id, ProjectRole.VIEWER)
        return self._inner.list_comparisons(project_id, viewer_id)

    def search_comparisons(  # noqa: PLR0913 -- one keyword per filter of a comparison list, and its page
        self,
        project_id: int,
        viewer_id: int,
        *,
        name_contains: str = "",
        which: models.ComparisonFilter = models.ComparisonFilter.VISIBLE,
        limit: int = 24,
        offset: int = 0,
    ) -> models.ComparisonList:
        self._require_self_or(viewer_id, Scope.ALL)
        self._require(project_id, ProjectRole.VIEWER)
        if which is models.ComparisonFilter.EDITABLE:
            # `_may_change_runs` as a filter the store can page: the viewer sees their own and shared ones, so an editor
            # of the project may change every one of them, and anyone else only their own.
            editor = self._can(project_id, ProjectRole.EDITOR)
            which = models.ComparisonFilter.VISIBLE if editor else models.ComparisonFilter.OWN
        return self._inner.search_comparisons(
            project_id, viewer_id, name_contains=name_contains, which=which, limit=limit, offset=offset
        )

    def search_comparisons_in(
        self, project_ids: Collection[int], viewer_id: int, *, name_contains: str, limit: int
    ) -> list[models.ComparisonSummary]:
        """As `search_comparisons`, of only the projects asked for that the viewer sees."""
        self._require_self_or(viewer_id, Scope.ALL)
        return self._inner.search_comparisons_in(
            [p for p in project_ids if self._can(p, ProjectRole.VIEWER)],
            viewer_id,
            name_contains=name_contains,
            limit=limit,
        )

    def comparison_summary(self, page_id: int) -> models.ComparisonSummary | None:
        if self.get_view(models.Page[Any, Any], page_id) is None:
            return None
        return self._inner.comparison_summary(page_id)

    def can_change_comparison_runs(self, page_id: int) -> bool:
        page = self.get_view(models.Page[Any, Any], page_id)
        return (
            page is not None
            and page.project_id is not None
            and page.owner_id is not None
            and self._may_change_runs(page.owner_id, page.project_id, shared=page.shared)
        )

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

    # -- Pins ---------------------------------------------------------------------------------------
    #
    # A pin is attribution, like a note: anyone who can see a run can pin it, as themselves, and
    # everyone in the project sees who did.

    def pin_runs(
        self,
        pinned_by: int,
        run_ids: frozenset[int],
        color: models.PinColor = models.PinColor.PLAIN,
    ) -> None:
        if pinned_by != self._actor.id:
            _forbid("a run can only be pinned as yourself")
        # Two reads whatever the number of runs: which experiments they are of, then who may see each (cached).
        experiments = self._inner.run_experiments(run_ids)
        if self._enforce and experiments.keys() != run_ids:
            _forbid("a run that does not exist, or is not yours to see, cannot be pinned")
        for project_id in {self._experiment_project(e) for e in set(experiments.values())}:
            self._require(project_id, ProjectRole.VIEWER)
        self._inner.pin_runs(pinned_by, run_ids, color)

    def unpin_runs(self, pinned_by: int, run_ids: frozenset[int], color: models.PinColor) -> None:
        self._require_self_or(pinned_by, Scope.ALL)
        self._inner.unpin_runs(pinned_by, run_ids, color)

    def run_experiments(self, run_ids: frozenset[int]) -> dict[int, int]:
        """Only the runs of experiments the actor can see: which experiment a run is of is not for anyone to ask."""
        found = self._inner.run_experiments(run_ids)
        return {
            run_id: experiment_id
            for run_id, experiment_id in found.items()
            if self._can(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        }

    def pins_of(
        self, experiment_id: int, *, run_ids: frozenset[int] | None
    ) -> dict[int, list[models.NamedPin]]:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.pins_of(experiment_id, run_ids=run_ids)

    def pinned_run_ids(  # noqa: PLR0913 -- the filters of `list_pinned_runs`, and how many runs
        self,
        project_id: int,
        *,
        colors: frozenset[models.PinColor] | None = None,
        pinned_by: frozenset[int] | None = None,
        experiment_id: int | None = None,
        name_contains: str = "",
        limit: int,
    ) -> list[int]:
        self._require(project_id, ProjectRole.VIEWER)
        return self._inner.pinned_run_ids(
            project_id,
            colors=colors,
            pinned_by=pinned_by,
            experiment_id=experiment_id,
            name_contains=name_contains,
            limit=limit,
        )

    def list_pinners(self, project_id: int) -> list[models.Pinner]:
        self._require(project_id, ProjectRole.VIEWER)
        return self._inner.list_pinners(project_id)

    def list_pinned_runs(  # noqa: PLR0913 -- one keyword per filter of the pinned-runs list
        self,
        project_id: int,
        *,
        colors: frozenset[models.PinColor] | None = None,
        pinned_by: frozenset[int] | None = None,
        experiment_id: int | None = None,
        name_contains: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> models.PinnedRuns:
        self._require(project_id, ProjectRole.VIEWER)
        return self._inner.list_pinned_runs(
            project_id,
            colors=colors,
            pinned_by=pinned_by,
            experiment_id=experiment_id,
            name_contains=name_contains,
            limit=limit,
            offset=offset,
        )

    def pin_counts(self, project_id: int) -> dict[models.PinColor, int]:
        self._require(project_id, ProjectRole.VIEWER)
        return self._inner.pin_counts(project_id)

    # -- Kept runs, retention -----------------------------------------------------------------------
    #
    # Keeping a run takes it out of retention's reach, so it is an editor's call; letting it go again, or deleting it,
    # is an owner's, or else an editor could undo what keeping is for. An admin (`Scope.ALL`) is an owner everywhere,
    # also where grants are not enforced, as a local deployment's one user is.

    def _require_owner(self, project_id: int | None) -> None:
        self._require_scope_or_role(Scope.ALL, project_id, ProjectRole.OWNER)

    def _run_projects(self, experiments: Mapping[int, int]) -> dict[int, set[int]]:
        """The project of each run (by the experiment each is of) that exists, with which of them are of it."""
        by_project: dict[int, set[int]] = {}
        for run_id, experiment_id in experiments.items():
            project_id = self._experiment_project(experiment_id)
            if project_id is not None:
                by_project.setdefault(project_id, set()).add(run_id)
        return by_project

    def _kept_among(self, experiments: Mapping[int, int]) -> frozenset[int]:
        """Which of the runs (by the experiment each is of) are kept, an experiment at a time."""
        by_experiment: dict[int, set[int]] = {}
        for run_id, experiment_id in experiments.items():
            by_experiment.setdefault(experiment_id, set()).add(run_id)
        return frozenset().union(
            *(self._inner.kept_runs(e, run_ids=frozenset(runs)) for e, runs in by_experiment.items())
        )

    def keep_runs(self, run_ids: frozenset[int], actor: models.User) -> None:
        by_project = self._run_projects(self._inner.run_experiments(run_ids))
        if self._enforce and set().union(*by_project.values()) != run_ids:
            _forbid("a run that does not exist, or is not yours to see, cannot be kept")
        for project_id in by_project:
            self._require(project_id, ProjectRole.EDITOR)
        self._inner.keep_runs(run_ids, self._bound(actor))

    def release_runs(self, run_ids: frozenset[int], actor: models.User) -> None:
        for project_id in self._run_projects(self._inner.run_experiments(run_ids)) or {None}:
            self._require_owner(project_id)
        self._inner.release_runs(run_ids, self._bound(actor))

    def kept_runs(self, experiment_id: int, *, run_ids: frozenset[int] | None) -> frozenset[int]:
        self._require(self._experiment_project(experiment_id), ProjectRole.VIEWER)
        return self._inner.kept_runs(experiment_id, run_ids=run_ids)

    # -- Plugins' scoped settings ---------------------------------------------------------------------
    #
    # Each key's rule is its plugin's (`register_setting`): a key with none is the system's alone.

    def _setting_rule(self, key: str) -> SettingRule:
        rule = self._setting_rules.get(key)
        if rule is None:
            _forbid(f"the setting {key!r}, which no plugin lets a request use")
        return rule

    def _require_role_or_admin(self, project_id: int | None, role: ProjectRole) -> None:
        """An owner's role takes an admin's scope where grants are not enforced, as everything an owner does there does."""
        if role is ProjectRole.OWNER:
            self._require_owner(project_id)
        else:
            self._require(project_id, role)

    def _read_settings(self, key: str, project_id: int) -> models.ScopedSettings:
        """A project's settings under `key`, read once per request: the header and a section of a page share one read."""
        if (key, project_id) not in self._settings:
            self._settings[key, project_id] = self._inner.get_scoped_settings(key, project_id)
        return self._settings[key, project_id]

    def get_scoped_settings(self, key: str, project_id: int) -> models.ScopedSettings:
        self._require(project_id, self._setting_rule(key).read)
        return self._read_settings(key, project_id)

    def all_scoped_settings(self, key: str) -> dict[int, models.ScopedSettings]:
        rule = self._setting_rule(key)
        return {
            project_id: found
            for project_id, found in self._inner.all_scoped_settings(key).items()
            if self._can(project_id, rule.read)
        }

    def put_scoped_setting(
        self,
        key: str,
        project_id: int,
        experiment_id: int | None,
        value: dict[str, Any] | None,
        actor: models.User,
    ) -> models.ScopedSetting | None:
        rule = self._setting_rule(key)
        self._require_role_or_admin(
            project_id, rule.write_project if experiment_id is None else rule.write_experiment
        )
        if value is not None and rule.check is not None:
            stored = self._read_settings(key, project_id)
            current = stored.project if experiment_id is None else stored.experiments.get(experiment_id)
            value = rule.check(
                SettingWrite(
                    project_id,
                    experiment_id,
                    value,
                    stored=current.value if current is not None else None,
                    admin=has_scope(self._actor, Scope.ALL),
                    actor_id=self._actor.id,
                )
            )
        self._settings.pop((key, project_id), None)  # what this request reads next is what it wrote
        return self._inner.put_scoped_setting(key, project_id, experiment_id, value, self._bound(actor))

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

    def purge_project(self, project_id: int, actor: models.User, *, pace: models.Pace | None = None) -> None:
        require_scope(self._actor, Scope.PURGE)
        self._inner.purge_project(project_id, self._bound(actor), pace=pace)

    def delete_experiment(self, experiment_id: int, actor: models.User) -> None:
        """An experiment that holds a kept run is an owner's to delete, as the kept run is."""
        project_id = self._experiment_project(experiment_id)
        self._require_scope_or_role(Scope.EXPERIMENT_DELETE, project_id, ProjectRole.EDITOR)
        if self._inner.kept_runs(experiment_id, run_ids=None):
            self._require_owner(project_id)
        self._inner.delete_experiment(experiment_id, self._bound(actor))

    def restore_experiment(self, experiment_id: int, actor: models.User) -> None:
        require_scope(self._actor, Scope.RESTORE)
        self._inner.restore_experiment(experiment_id, self._bound(actor))

    def purge_experiment(
        self, experiment_id: int, actor: models.User, *, pace: models.Pace | None = None
    ) -> None:
        require_scope(self._actor, Scope.PURGE)
        self._inner.purge_experiment(experiment_id, self._bound(actor), pace=pace)

    def delete_run(self, run_id: int, actor: models.User) -> None:
        """A kept run is an owner's to delete."""
        project_id = self._run_project(run_id)
        self._require_scope_or_role(Scope.RUN_DELETE, project_id, ProjectRole.EDITOR)
        if self._kept_among(self._inner.run_experiments(frozenset({run_id}))):
            self._require_owner(project_id)
        self._inner.delete_run(run_id, self._bound(actor))

    def delete_runs(
        self, run_ids: frozenset[int], actor: models.User, *, spare_kept: bool = False
    ) -> frozenset[int]:
        # Which experiment each is of in one read, then their projects, then who may delete in each, and an owner where
        # any is kept (unless kept runs are spared: they are not deleted); a run that is not there is for the store to
        # refuse.
        experiments = self._inner.run_experiments(run_ids)
        kept = frozenset() if spare_kept else self._kept_among(experiments)
        by_project: dict[int | None, set[int]] = {**self._run_projects(experiments)} or {None: set()}
        for project_id, runs in by_project.items():
            self._require_scope_or_role(Scope.RUN_DELETE, project_id, ProjectRole.EDITOR)
            if runs & kept:
                self._require_owner(project_id)
        return self._inner.delete_runs(run_ids, self._bound(actor), spare_kept=spare_kept)

    def restore_run(self, run_id: int, actor: models.User) -> None:
        require_scope(self._actor, Scope.RESTORE)
        self._inner.restore_run(run_id, self._bound(actor))

    def purge_run(self, run_id: int, actor: models.User, *, pace: models.Pace | None = None) -> None:
        require_scope(self._actor, Scope.PURGE)
        self._inner.purge_run(run_id, self._bound(actor), pace=pace)

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

    def trashed_before(self, entity_type: models.EntityType, cutoff: datetime, *, limit: int) -> list[int]:
        # What the Trash has held longest is for the task that empties it, which no user is behind.
        require_scope(self._actor, Scope.PURGE)
        return self._inner.trashed_before(entity_type, cutoff, limit=limit)


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
