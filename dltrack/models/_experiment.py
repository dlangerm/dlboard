"""An experiment within a project."""

import pendulum
from pydantic import AwareDatetime, BaseModel, Field


class NewExperiment(BaseModel, frozen=True, extra="forbid"):
    """An experiment within a project."""

    project_id: int
    """The project ID to use for this experiment."""

    name: str = ""
    """The name of the experiment."""

    description: str = ""
    """A description of the experiment."""

    created_by: int | None = None
    """The user who created this experiment, if known."""

    created_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))
    """When this experiment was created."""


class Experiment(NewExperiment, frozen=True, extra="forbid"):
    """An experiment stored in the database."""

    id: int
    """The ID of the experiment."""

    deleted_by: int | None = None
    """The user who soft-deleted this experiment, if it's been deleted."""

    deleted_at: AwareDatetime | None = None
    """When this experiment was soft-deleted, if at all. See `Project.deleted_at`."""
