"""A run of an experiment."""

import pendulum
from pydantic import AwareDatetime, BaseModel, Field


class NewRun(BaseModel, frozen=True, extra="forbid"):
    """A run within an experiment."""

    experiment_id: int
    """The experiment ID to use for this run."""

    name: str | None = None
    """A human-readable name for this run. Falls back to `Run {id}` in the UI when unset."""

    created_by: int | None = None
    """The user who created this run, if known."""

    created_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))
    """When this run was created."""


class Run(NewRun, frozen=True, extra="forbid"):
    """A run stored in the database."""

    id: int
    """The ID of the run."""

    deleted_by: int | None = None
    """The user who soft-deleted this run, if it's been deleted."""

    deleted_at: AwareDatetime | None = None
    """When this run was soft-deleted, if at all. See `Project.deleted_at`."""
