"""Defines the protocol for interacting with an arbitrary data store."""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    from collections.abc import Iterable

    from dltrack import models


class DataStore[**P](typing.Protocol):
    """Any data store."""

    @classmethod
    def get_or_create(cls, *args: P.args, **kwargs: P.kwargs) -> typing.Self:
        """Initialize a data store."""
        ...

    def create_project(self, project: models.NewProject) -> models.Project | None:
        """Create a project."""
        ...

    def get_project(self, database_id: int) -> models.Project:
        """Get a project."""
        ...

    def get_projects(self) -> typing.Iterator[models.Project]:
        """Get all projects."""
        ...

    def create_experiment(self, experiment: models.NewExperiment) -> models.Experiment | None:
        """Create an experiment within a project."""
        ...

    def get_experiment(self, database_id: int) -> models.Experiment | None:
        """Get an experiment by id."""
        ...

    def get_experiments(self, project_id: int) -> typing.Iterator[models.Experiment]:
        """Get all experiments for a project."""
        ...

    def get_all_experiments(self) -> typing.Iterator[models.Experiment]:
        """Get all experiments across projects."""
        ...

    def log_metrics(self, metric: Iterable[models.LoggedMetrics]) -> None:
        """Log metrics to the data store."""
        ...

    def fetch_metrics(
        self,
        experiment_id: int,
        metric_name_match: str | None = None,
        step_range: slice | None = None,
    ) -> typing.Iterator[models.LoggedMetrics]:
        """Fetch metrics for a particular table name, optionally matching a topic string."""
        ...
