"""An experiment within a project."""

from pydantic import BaseModel


class NewExperiment(BaseModel, frozen=True, extra="forbid"):
    """An experiment within a project."""

    project_id: int
    """The project ID to use for this experiment."""


class Experiment(NewExperiment, frozen=True, extra="forbid"):
    """An experiment stored in the database."""

    id: int
    """The ID of the experiment."""
