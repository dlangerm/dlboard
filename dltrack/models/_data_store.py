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

    def create_project(self, project: models.NewProject) -> models.Project:
        """Create a project."""
        ...

    def get_project(self, database_id: int) -> models.Project:
        """Get a project."""
        ...

    def get_projects(self) -> typing.Iterator[models.Project]:
        """Get all projects."""
        ...

    def update_project(self, project: models.Project) -> models.Project:
        """Update a project."""
        ...

    def create_experiment(self, experiment: models.NewExperiment) -> models.Experiment:
        """Create an experiment within a project."""
        ...

    def get_experiment(self, database_id: int) -> models.Experiment | None:
        """Get an experiment by id."""
        ...

    def get_experiments(self, project_id: int) -> typing.Iterator[models.Experiment]:
        """Get all experiments for a project."""
        ...

    def update_experiment(self, experiment: models.Experiment) -> models.Experiment:
        """Update an experiment."""
        ...

    def create_run(self, run: models.NewRun) -> models.Run:
        """Create a new run for an experiment."""
        ...

    def log_metrics(self, metric: Iterable[models.LoggedMetrics]) -> None:
        """Log metrics to the data store."""
        ...

    def fetch_metrics(
        self,
        experiment_id: int,
        run_id: int | None = None,
        metric_name_match: set[str] | None = None,
        step_range: slice | None = None,
    ) -> typing.Iterator[models.LoggedMetrics]:
        """Fetch metrics for a particular table name, optionally matching a topic string."""
        ...

    def log_hyperparams(self, hyperparams: models.NewHyperParams) -> models.HyperParams:
        """Log hyperparameters to the data store."""
        ...

    def fetch_hyperparams(self, experiment_id: int) -> typing.Iterator[models.HyperParams]:
        """Fetch hyperparameters for all runs for a particular experiment_id."""
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

    def log_artifact_refs(self, artifacts: Iterable[models.Artifact]) -> None:
        """Log a set of artifacts."""
        ...

    def fetch_artifacts(
        self,
        keys: set[str] | None = None,
        run_id: int | None = None,
        experiment_id: int | None = None,
        step: int | None = None,
        fname: str | None = None,
    ) -> typing.Iterator[models.Artifact]:
        """Get artifacts for a run or experiment."""
        ...

    def delete_project(self, project_id: int, actor_id: int) -> None:
        """Soft-delete a project and cascade to its experiments, runs, and artifacts."""
        ...

    def restore_project(self, project_id: int) -> None:
        """Restore a soft-deleted project and everything deleted with it."""
        ...

    def purge_project(self, project_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted project. Requires `Scope.PURGE`."""
        ...

    def delete_experiment(self, experiment_id: int, actor_id: int) -> None:
        """Soft-delete an experiment and cascade to its runs and artifacts."""
        ...

    def restore_experiment(self, experiment_id: int) -> None:
        """Restore a soft-deleted experiment and everything deleted with it."""
        ...

    def purge_experiment(self, experiment_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted experiment. Requires `Scope.PURGE`."""
        ...

    def delete_run(self, run_id: int, actor_id: int) -> None:
        """Soft-delete a run and cascade to its artifacts."""
        ...

    def restore_run(self, run_id: int) -> None:
        """Restore a soft-deleted run and everything deleted with it."""
        ...

    def purge_run(self, run_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted run. Requires `Scope.PURGE`."""
        ...

    def delete_artifact(self, artifact_id: int, actor_id: int) -> None:
        """Soft-delete a single artifact."""
        ...

    def restore_artifact(self, artifact_id: int) -> None:
        """Restore a soft-deleted artifact."""
        ...

    def purge_artifact(self, artifact_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted artifact. Requires `Scope.PURGE`."""
        ...

    def list_deleted_projects(self) -> typing.Iterator[models.Project]:
        """List soft-deleted projects, for a trash/admin view."""
        ...

    def list_deleted_experiments(self) -> typing.Iterator[models.Experiment]:
        """List soft-deleted experiments, for a trash/admin view."""
        ...

    def list_deleted_runs(self) -> typing.Iterator[models.Run]:
        """List soft-deleted runs, for a trash/admin view."""
        ...

    def list_deleted_artifacts(self) -> typing.Iterator[models.Artifact]:
        """List soft-deleted artifacts, for a trash/admin view."""
        ...


class ArtifactStore[**P](typing.Protocol):
    """An artifact store for files and arbitrary byte-like data."""

    protocol: typing.ClassVar[str]
    """The protocol for the artifact store, used to create urls."""

    @classmethod
    def get_or_create(cls, *args: P.args, **kwargs: P.kwargs) -> typing.Self:
        """Initialize a data store."""
        ...

    def log_artifacts(
        self,
        artifacts: Iterable[models.NewArtifact],
        files: typing.Mapping[str, FileStorage],
    ) -> None:
        """Log a set of artifacts."""
        ...

    def download_artifact(self, ref: AnyUrl) -> Response:
        """Download an artifact given a url."""
        ...
