"""Defines the protocol for interacting with an arbitrary data store."""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    from collections.abc import Iterable

    from flask import Response
    from pydantic import AnyUrl
    from werkzeug.datastructures import FileStorage

    from dltrack import models


class DataStore[**P](typing.Protocol):
    """Any data store."""

    @classmethod
    def get_or_create(cls, *args: P.args, **kwargs: P.kwargs) -> typing.Self:
        """Initialize a data store."""
        ...

    def get_or_create_user(self, username: str) -> models.User:
        """Get or create a user by username. Brand new users are granted no scopes."""
        ...

    def update_user(self, user: models.User) -> models.User:
        """Update a user, e.g. to grant/revoke scopes. For admin-driven user management."""
        ...

    def create_project(self, project: models.NewProject) -> models.Project:
        """Create a project."""
        ...

    def get_or_create_project(
        self, name: str, description: str = "", created_by: int | None = None
    ) -> models.Project:
        """Get the project named `name`, creating it (with `description`) if it doesn't exist yet."""
        ...

    def get_project(self, database_id: int) -> models.Project:
        """Get a project."""
        ...

    def get_projects(self) -> typing.Iterator[models.Project]:
        """Get all projects."""
        ...

    def get_project_stats(self) -> dict[int, models.ProjectStats]:
        """Every (non-deleted) project's activity, keyed by project id -- in one read, for a listing."""
        ...

    def update_project(self, project: models.Project) -> models.Project:
        """Update a project."""
        ...

    def create_experiment(self, experiment: models.NewExperiment) -> models.Experiment:
        """Create an experiment within a project."""
        ...

    def get_or_create_experiment(
        self,
        project_id: int,
        name: str = "default",
        created_by: int | None = None,
        source: models.ExperimentSource | None = None,
    ) -> models.Experiment:
        """
        Get the named experiment within a project, creating it if it doesn't exist yet.

        `source` only applies the first time `name` is seen within `project_id` -- once the
        experiment exists, later calls just reuse it as-is.
        """
        ...

    def get_experiment(self, database_id: int) -> models.Experiment | None:
        """Get an experiment by id."""
        ...

    def get_experiments(self, project_id: int) -> typing.Iterator[models.Experiment]:
        """Get all experiments for a project."""
        ...

    def get_experiment_stats(self, project_id: int) -> dict[int, models.ActivityStats]:
        """Each of a project's (non-deleted) experiments' activity, keyed by experiment id -- in one read."""
        ...

    def update_experiment(self, experiment: models.Experiment) -> models.Experiment:
        """Update an experiment."""
        ...

    def create_run(self, run: models.NewRun) -> models.Run:
        """Create a new run for an experiment."""
        ...

    def get_runs(
        self, experiment_id: int, *, limit: int = 1000, offset: int = 0
    ) -> typing.Iterator[models.Run]:
        """Get a page of an experiment's (non-deleted) runs, most recently created first."""
        ...

    def log_metrics(self, metric: Iterable[models.LoggedMetrics]) -> None:
        """Log metrics to the data store."""
        ...

    def fetch_metrics(
        self,
        experiment_id: int,
        *,
        keys: frozenset[str] | None = None,
        exclude_run_ids: frozenset[int] = frozenset(),
    ) -> models.MetricFrame:
        """An experiment's (non-deleted runs') metrics -- only `keys`, if given, else every metric."""
        ...

    def summarize_metric_keys(self, experiment_id: int) -> list[models.MetricKeySummary]:
        """
        Every metric key logged in an experiment (non-deleted runs), sorted, without fetching values.

        For callers (a column picker, chart suggestions) that only need to know what's *available*
        -- fetching every metric row via `fetch_metrics` for that is needlessly expensive once an
        experiment has any real volume of logged steps.
        """
        ...

    def log_hyperparams(self, hyperparams: models.NewHyperParams) -> models.HyperParams:
        """Log hyperparameters to the data store."""
        ...

    def fetch_hyperparams(
        self, experiment_id: int, *, exclude_run_ids: frozenset[int] = frozenset()
    ) -> typing.Iterator[models.HyperParams]:
        """Every (non-deleted) run's hyperparameters for an experiment."""
        ...

    def get_or_create_page[Dataframe, Panel, Chart](
        self,
        page_type: type[models.Page[Dataframe, Panel, Chart]],
        *,
        run_id: int | None = None,
        experiment_id: int | None = None,
        project_id: int | None = None,
        new_page_type: type[models.NewPage[Dataframe, Chart]] | None = None,
    ) -> models.Page[Dataframe, Panel, Chart]:
        """Fetch the pages for a run, experiment, or project."""
        ...

    def update_page[Dataframe, Panel, Chart](
        self, page: models.Page[Dataframe, Panel, Chart]
    ) -> models.Page[Dataframe, Panel, Chart]:
        """Update a page."""
        ...

    def create_view[Dataframe, Panel, Chart](
        self, page_type: type[models.Page[Dataframe, Panel, Chart]], view: models.NewPage[Dataframe, Chart]
    ) -> models.Page[Dataframe, Panel, Chart]:
        """Save a named, owned view of a page (see `NewPage.owner_id`) as its own page."""
        ...

    def get_view[Dataframe, Panel, Chart](
        self, page_type: type[models.Page[Dataframe, Panel, Chart]], view_id: int
    ) -> models.Page[Dataframe, Panel, Chart] | None:
        """A named view by id, or `None` if there's no such view."""
        ...

    def list_views(self, experiment_id: int, owner_id: int) -> list[models.ViewSummary]:
        """`owner_id`'s views of an experiment's page."""
        ...

    def delete_view(self, view_id: int, owner_id: int) -> None:
        """Delete one of `owner_id`'s views; never anyone else's, nor a shared page."""
        ...

    def log_artifact_refs(self, artifacts: Iterable[models.Artifact]) -> None:
        """Log a set of artifacts."""
        ...

    def fetch_artifacts(
        self,
        experiment_id: int,
        *,
        keys: frozenset[str] | None = None,
        exclude_run_ids: frozenset[int] = frozenset(),
    ) -> typing.Iterator[models.Artifact]:
        """An experiment's (non-deleted) artifact metadata, not bytes -- only `keys`, if given."""
        ...

    def delete_project(self, project_id: int, actor: models.User) -> None:
        """Soft-delete a project and cascade to its experiments, runs, and artifacts. Requires `Scope.PROJECT_DELETE`."""
        ...

    def restore_project(self, project_id: int, actor: models.User) -> None:
        """Restore a soft-deleted project and everything deleted with it. Requires `Scope.RESTORE`."""
        ...

    def purge_project(self, project_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted project. Requires `Scope.PURGE`."""
        ...

    def delete_experiment(self, experiment_id: int, actor: models.User) -> None:
        """Soft-delete an experiment and cascade to its runs and artifacts. Requires `Scope.EXPERIMENT_DELETE`."""
        ...

    def restore_experiment(self, experiment_id: int, actor: models.User) -> None:
        """Restore a soft-deleted experiment and everything deleted with it. Requires `Scope.RESTORE`."""
        ...

    def purge_experiment(self, experiment_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted experiment. Requires `Scope.PURGE`."""
        ...

    def delete_run(self, run_id: int, actor: models.User) -> None:
        """Soft-delete a run and cascade to its artifacts. Requires `Scope.RUN_DELETE`."""
        ...

    def restore_run(self, run_id: int, actor: models.User) -> None:
        """Restore a soft-deleted run and everything deleted with it. Requires `Scope.RESTORE`."""
        ...

    def purge_run(self, run_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted run. Requires `Scope.PURGE`."""
        ...

    def delete_artifact(self, artifact_id: int, actor: models.User) -> None:
        """Soft-delete a single artifact. Requires `Scope.ARTIFACT_DELETE`."""
        ...

    def restore_artifact(self, artifact_id: int, actor: models.User) -> None:
        """Restore a soft-deleted artifact. Requires `Scope.RESTORE`."""
        ...

    def purge_artifact(self, artifact_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted artifact. Requires `Scope.PURGE`."""
        ...

    def list_deleted_projects(self, limit: int = 100, offset: int = 0) -> typing.Iterator[models.Project]:
        """List soft-deleted projects, most recently deleted first, for a trash/admin view."""
        ...

    def list_deleted_experiments(
        self, limit: int = 100, offset: int = 0
    ) -> typing.Iterator[models.Experiment]:
        """List soft-deleted experiments, most recently deleted first, for a trash/admin view."""
        ...

    def list_deleted_runs(self, limit: int = 100, offset: int = 0) -> typing.Iterator[models.Run]:
        """List soft-deleted runs, most recently deleted first, for a trash/admin view."""
        ...

    def list_deleted_artifacts(self, limit: int = 100, offset: int = 0) -> typing.Iterator[models.Artifact]:
        """List soft-deleted artifacts, most recently deleted first, for a trash/admin view."""
        ...

    def list_audit_log(
        self, actor: models.User, limit: int = 100, offset: int = 0
    ) -> typing.Iterator[models.AuditLogEntry]:
        """List audit log entries, most recent first, for a trash/admin view. Requires `Scope.AUDIT_LOG_READ`."""
        ...

    def list_pending_artifact_purges(
        self, limit: int = 100, offset: int = 0
    ) -> typing.Iterator[models.ArtifactPurgeTask]:
        """List artifact blobs still waiting to be deleted from the `ArtifactStore`, oldest first."""
        ...

    def count_pending_artifact_purges(self) -> int:
        """Count artifact blobs still waiting to be deleted. 0 means the last purge fully cleaned up."""
        ...

    def complete_artifact_purge(self, task_id: int) -> None:
        """Record that a queued blob deletion succeeded by deleting its task row."""
        ...

    def fail_artifact_purge(self, task_id: int, error: str) -> None:
        """Record that a queued blob deletion failed. The task stays pending and is retried later."""
        ...


class ArtifactStore[**P](typing.Protocol):
    """An artifact store for files and arbitrary byte-like data."""

    protocol: typing.ClassVar[str]
    """The protocol for the artifact store, used to create urls."""

    @classmethod
    def get_or_create(cls, *args: P.args, **kwargs: P.kwargs) -> typing.Self:
        """Initialize a data store."""
        ...

    def log_artifacts(self, artifacts: Iterable[tuple[models.NewArtifact, FileStorage]]) -> None:
        """Log a set of artifacts, each paired with its own uploaded file."""
        ...

    def download_artifact(self, ref: AnyUrl) -> Response:
        """Download an artifact given a url."""
        ...

    def delete_artifact(self, ref: AnyUrl) -> None:
        """Permanently delete one artifact blob. Idempotent -- a blob that's already gone is not an error."""
        ...
