"""Defines the protocol for interacting with an arbitrary data store."""

import typing

from dltrack._models import experiment, project  # noqa: TC001


class DataStore[**P](typing.Protocol):
    """Any data store."""

    @classmethod
    def get_or_create(cls, *args: P.args, **kwargs: P.kwargs) -> typing.Self:
        """Initialize a data store."""
        ...

    def create_project(self, project: project.NewProject) -> project.Project | None:
        """Create a project."""
        ...

    def get_project(self, database_id: project.ProjectID) -> project.Project:
        """Get a project."""
        ...

    def get_projects(self) -> typing.Iterator[project.Project]:
        """Get all projects."""
        ...

    def create_experiment(self, experiment: experiment.NewExperiment) -> experiment.Experiment | None:
        """Create an experiment within a project."""
        ...

    def get_experiment(self, database_id: experiment.ExperimentID) -> experiment.Experiment | None:
        """Get an experiment by id."""
        ...
