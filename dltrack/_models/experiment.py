"""An experiment within a project."""

import typing

from pydantic import BaseModel

from dltrack._models import project  # noqa: TC001

type ExperimentID = int


class NewExperiment(BaseModel, frozen=True, extra="forbid"):
    """An experiment within a project."""

    project_id: project.ProjectID
    """The project ID to use for this experiment."""


class Experiment(NewExperiment, frozen=True, extra="forbid"):
    """An experiment stored in the database."""

    id: ExperimentID | None = None
    """The ID of the experiment."""

    def with_id(self, id: ExperimentID) -> typing.Self:
        return self.model_copy(update={"id": id})
